---
license: apache-2.0
library_name: timm
---

# WD EVA02 Tagger 2026 Canary

**Training Data cutoff**: 20260518, danbooru ID 11403645
Tag metadata source:
[u-haru/danbooru-tags-20260518](https://huggingface.co/datasets/u-haru/danbooru-tags-20260518).

**Reference**: base model
[SmilingWolf/wd-eva02-large-tagger-v3](https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3)


## Model Bio


- 5,999 new tags added (2,205 character + 3,794 general), 16,473 total.
- Trained on images with IDs above 7,220,105(Original model knowledge cutoff), modulo 0000-0899
- Validated modulo 0900-0949
- Per-label operating-point recentering post-training

## Validation results

`P=R: threshold = 0.6094, F1 = 0.5416`

## Inference code examples

For timm: https://github.com/neggles/wdv3-timm

```python
import timm
from safetensors.torch import load_file

model = timm.create_model("eva02_large_patch14_448", pretrained=False, num_classes=16473)
model.load_state_dict(load_file("model.safetensors"), strict=True)
model.eval()
```
