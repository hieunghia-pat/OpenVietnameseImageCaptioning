# training configuration
checkpoint_path = "saved_models"
start_from = None
learning_rate = 1.
epochs = 20
warmup = 10000
xe_base_lr = 1e-4
rl_base_lr = 5e-6
refine_epoch_rl = 28
xe_least = 15
xe_most = 20
min_freq = 1

# model configuration
model_name = "ort_using_region"
total_memory = 40
nhead = 8
nlayers = 3
d_model = 512
d_k = 64
d_v = 64
d_ff = 2048
d_feature = 2048
dropout = .1
beam_size = 5

# dataset configuration
train_json_path = "features/annotations/vieCap4H/viecap4h_captions_train2017.json"
val_json_path = "features/annotations/vieCap4H/viecap4h_captions_val2017.json"
public_test_json_path = "features/annotations/vieCap4H/viecap4h_captions_public_test2017.json"
private_test_json_path = "features/annotations/vieCap4H/viecap4h_captions_private_test2017.json"
feature_path = "features/region_features/UIT-ViIC/faster_rcnn"
batch_size = 16
workers = 2

# sample submission configuration
sample_public_test_json_data = "sample_public_test_submission.json"
sample_private_test_json_data = "sample_private_test_submission.json"