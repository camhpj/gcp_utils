from google.cloud import firestore
from gcp_utils.tools.preprocess import validate_window, rescale_data
from gcp_utils.tools.predict import get_inputs, predict_bp

client = firestore.Client()

CONFIG = dict(
    scaler_path='gcp_utils/data/mimic3-min-max-2022-11-08.pkl',
    checks=['snr', 'hr', 'beat'],
    fs=125,                                 # sampling frequency
    win_len=256,                            # window length
    freq_band=[0.5, 8.0],                   # bandpass frequencies
    sim=0.6,                                # similarity threshold
    snr=2.0,                                # SNR threshold
    hr_freq_band=[0.667, 3.0],              # valid heartrate frequency band in Hz
    hr_delta=1/6,                           # maximum heart rate difference between ppg, abp
    dbp_bounds=[20, 130],                   # upper and lower threshold for DBP
    sbp_bounds=[50, 225],                   # upper and lower threshold for SBP
    windowsize=1,                           # windowsize for rolling mean
    ma_perc=20,                             # multiplier for peak detection
    beat_sim=0.2,                           # lower threshold for beat similarity
)

# Validate window
def onNewSample(data, context):
    path_parts = context.resource.split('/documents/')[1].split('/')
    collection_path = path_parts[0]
    document_path = '/'.join(path_parts[1:])

    affected_doc = client.collection(collection_path).document(document_path)
    ppg_raw = [float(x['doubleValue']) for x in data["value"]["fields"]["ppg_raw"]["arrayValue"]['values']]

    result = validate_window(
        ppg=ppg_raw,
        config=CONFIG,
    )
    affected_doc.update({
        u'status': result['status'],
        u'ppg': result['ppg'],
        u'vpg': result['vpg'],
        u'apg': result['apg'],
        u'ppg_scaled': result['ppg_scaled'],
        u'vpg_scaled': result['vpg_scaled'],
        u'apg_scaled': result['apg_scaled'],
    })

# Make prediction on ppg using enceladus (vital-bee-206-serving)
def onValidSample(data, context):
    path_parts = context.resource.split('/documents/')[1].split('/')
    collection_path = path_parts[0]
    document_path = '/'.join(path_parts[1:])

    affected_doc = client.collection(collection_path).document(document_path)

    status = str(data["value"]["fields"]["stats"]["stringValue"])
    if status == 'valid':
        instance_dict = get_inputs(data)
        abp_scaled = predict_bp(
            project="123543907199",
            endpoint_id="4207052545266286592",
            location="us-central1",
            instances=instance_dict,
        )
        abp = rescale_data(CONFIG['scaler_path'], abp_scaled)
        affected_doc.update({
            u'status': 'predicted',
            u'abp_scaled': abp_scaled,
            u'abp': abp,
        })
