import numpy as np
import pickle as pkl
from typing import Tuple
from database_tools.tools.dataset import ConfigMapper, Window
from database_tools.processing.modify import bandpass
from database_tools.processing.utils import resample_signal
from database_tools.processing.detect import detect_peaks

def process_frame(red_frame: list, ir_frame: list, cm: ConfigMapper) -> dict:
    """
    Steps
    -----
    1. Clean ppg data and get peaks
    2. Resample (bpm fs -> mimic3 fs)
    3. Flip (correct ppg direction)
    4. Filter (Remove noise)
    5. Split into windows
    """
    # 1
    red_frame = np.array(red_frame, dtype=np.float32)
    ir_frame = np.array(ir_frame, dtype=np.float32)

    # 2
    red_clean, red_idx = _preprocess_ppg(red_frame, cm)
    ir_clean, ir_idx = _preprocess_ppg(ir_frame, cm)

    # 3
    red_resamp = resample_signal(red_clean, fs_old=cm.deploy.bpm_fs, fs_new=cm.data.fs)
    ir_resamp = resample_signal(ir_clean, fs_old=cm.deploy.bpm_fs, fs_new=cm.data.fs)

    # 4
    red_flip = _flip_signal(red_resamp)
    ir_flip = _flip_signal(ir_resamp)

    # 5
    red_filt = bandpass(red_flip, low=cm.data.freq_band[0], high=cm.data.freq_band[1], fs=cm.deploy.bpm_fs, method='butter')
    ir_filt = bandpass(ir_flip, low=cm.data.freq_band[0], high=cm.data.freq_band[1], fs=cm.deploy.bpm_fs, method='butter')

    # 6
    windows = _split_frame(sig=ir_filt, n=int(ir_filt.shape[0] / cm.data.win_len))

    result = {
        'red_frame_spo2': red_clean.tolist(),
        'ir_frame_spo2': ir_clean.tolist(),
        'red_idx': red_idx,
        'ir_idx': ir_idx,
        'red_frame_for_presentation': red_filt.tolist(),
        'ir_frame_for_presentation': ir_filt.tolist(),
        'frame_for_prediction': ir_filt.tolist(),
        'windows': windows,
    }
    return result

def _preprocess_ppg(sig: list, cm: ConfigMapper):
    """Remove data too far from the signal medium (handles large noise and motion artifacts).
       Also, calculate peaks in longest run of good data.
    """
    # Prep data
    sig = np.array(sig, dtype=np.float32).reshape(-1)
    sig[np.isnan(sig)] = 0

    # Remove signal outliers
    sig = _remove_sig_outliers(sig, cm.deploy.sig_amp_thresh)
    # return sig, dict()

    # get, remove outliers from, and order peaks
    try:
        idx = detect_peaks(bandpass(sig, low=0.5, high=8.0, fs=cm.deploy.bpm_fs))

        peaks, troughs = idx['peaks'], idx['troughs']
        peaks = _remove_peak_outliers(sig, peaks, cm.deploy.peak_amp_thresh, cm.deploy.peak_dist_thresh)
        troughs = _remove_peak_outliers(sig, troughs, cm.deploy.peak_amp_thresh, cm.deploy.peak_dist_thresh)

        idx = dict(peaks=peaks, troughs=troughs)
    except Exception as e:
        idx = dict(peaks=[], troughs=[])
    return sig, idx

def _remove_sig_outliers(sig, amp_thresh, buffer=50):
    med = np.median(sig)
    mask = (sig > (med + amp_thresh)) | (sig < (med - amp_thresh))

    temp = sig.copy()
    temp[np.where(mask)] = 0
    temp[np.where(~mask)] = 1
    run_values, run_starts, run_lengths = _find_runs(temp)

    if run_lengths.shape[0] < 2:
        return sig
    else:
        for idx, val in enumerate(run_values):
            if (val == 0) | ( (val == 1) & (run_lengths[idx] < 800) ):
                if idx == (run_values.shape[0] - 1):
                    i = run_starts[idx]
                    sig[i::] = med
                else:
                    i, j = run_starts[idx], run_starts[idx+1]
                    sig[i:j+buffer] = med
        return sig

def _remove_peak_outliers(sig, idx, amp_thresh, dist_thresh):
    # remove indices whose amplitude is too far from mean
    values = sig[idx]
    mean = np.mean(values)
    mask = np.where( (values < (mean + amp_thresh)) & (values > (mean - amp_thresh)) )
    idx = idx[mask]

    # remove indices that are too from from each other
    diff = np.diff(idx, prepend=idx[0] - 10000, append=idx[-1] + 10000)
    delta = int(np.mean(diff[1:-1]))

    valid = []
    for i, distance1 in enumerate(diff[0:-1]):
        distance2 = diff[i+1]
        if (distance1 <= (delta + dist_thresh)) & (distance2 <= (delta + dist_thresh)):
            valid.append(idx[i])
        else:
            if len(valid) > 0:
                break
    return valid

def _find_runs(x):
    n = x.shape[0]
    loc_run_start = np.empty(n, dtype=bool)
    loc_run_start[0] = True
    np.not_equal(x[:-1], x[1:], out=loc_run_start[1:])
    
    run_starts = np.nonzero(loc_run_start)[0]
    run_values = x[loc_run_start]
    run_lengths = np.diff(np.append(run_starts, n))
    return (run_values, list(run_starts), run_lengths)

def _flip_signal(sig):
    """Flip signal data but subtracting the maximum value."""
    flipped = np.max(sig) - sig
    return flipped

def _split_frame(sig: np.ndarray, n: int) -> list:
    """Split list into n lists.

    Args:
        sig (list): Data.
        n (int): Number of lists.

    Returns:
        n_sigs (list): Data split in to n lists.
    """
    n_sigs = [s.tolist() for s in np.split(sig, n)]
    return n_sigs

def validate_window(ppg: list, cm: ConfigMapper, force_valid: bool = False) -> dict:
    # convert to numpy array
    ppg = np.array(ppg, dtype=np.float32)

    # bpm_scaling
    with open(cm.deploy.bpm_scaler_path, 'rb') as f:
        scaler = pkl.load(f)
    ppg = np.divide(ppg - scaler[0], scaler[1] - scaler[0])

    # validate window with call to win.valid
    win = Window(ppg, cm, checks=cm.data.checks)
    win.get_peaks()
    status = 'valid' if win.valid else 'invalid'

    # debug mode
    if cm.deploy.force_valid:
        status = 'valid'

    # get model inputs if valid
    if status == 'valid':
        vpg, apg = _get_ppg_derivatives(ppg)
    else:
        vpg, apg = np.array([]), np.array([])

    # scale data with mimic3 training minmax scaler
    ppg_s, vpg_s, apg_s = _scale_data(cm.deploy.enceladus_scaler_path, ppg, vpg, apg)

    flat_lines = not win._flat_check
    result = {
        'status': str(status),
        'vpg': vpg.tolist(),
        'apg': apg.tolist(),
        'ppg_scaled': ppg_s.tolist(),
        'vpg_scaled': vpg_s.tolist(),
        'apg_scaled': apg_s.tolist(),
        'f0': float(win.f0),
        'snr': float(win.snr),
        'beat_sim': float(win.beat_sim),
        'notches': bool(win._notch_check),
        'flat_lines': bool(flat_lines),
    }
    return result

def _get_ppg_derivatives(ppg: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    vpg = np.gradient(ppg, axis=0)  # 1st derivative of ppg
    apg = np.gradient(vpg, axis=0)  # 2nd derivative of vpg
    return (vpg, apg)

def _scale_data(path: str, ppg: np.ndarray, vpg: np.ndarray, apg: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    with open(path, 'rb') as f:
        scalers, _ = pkl.load(f)
    ppg_scaler = scalers['ppg']
    vpg_scaler = scalers['vpg']
    apg_scaler = scalers['apg']
    ppg_s = np.divide(ppg - ppg_scaler[0], ppg_scaler[1] - ppg_scaler[0])
    vpg_s = np.divide(vpg - vpg_scaler[0], vpg_scaler[1] - vpg_scaler[0])
    apg_s = np.divide(apg - apg_scaler[0], apg_scaler[1] - apg_scaler[0])
    return (ppg_s, vpg_s, apg_s)
