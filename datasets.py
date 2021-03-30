import pickle
from pathlib import Path
from typing import Tuple, Dict, List

import numpy as np
import pandas as pd
import torch as th

GT_NAMES = {
    "train": "annotation_training.pkl",
    "valid": "annotation_validation.pkl",
    "test": "annotation_test.pkl",
}

SET_SIZE = {"train": 6000, "valid": 2000, "test": 2000}

IMPRESSIONV2_DIR = Path("/impressionv2")
EMBEDDING_DIR = Path("/mbalazsdb")


class TensorDatasetWithTransformer(th.utils.data.Dataset):
    def __init__(self, tensor_dataset, transform=None):
        self.tensor_dataset = tensor_dataset
        self.transform = transform

    def __getitem__(self, index):
        sample = self.tensor_dataset[index]
        if self.transform:
            sample = self.transform(sample)

        return sample

    def __len__(self):
        return len(self.tensor_dataset)


class SamplerTransform:
    def __init__(self, srA, srF, srT, is_random=False):
        self.srA = srA
        self.srF = srF
        self.srT = srT
        self.is_random = is_random

    def __call__(self, x):
        audio, face, text, label = x
        al = audio.shape[0]
        fl = face.shape[0]
        tl = text.shape[0]
        assert al == 1526
        assert fl == 459
        assert tl == 60
        if self.srA is None:
            self.srA = al
        if self.srF is None:
            self.srF = fl
        if self.srT is None:
            self.srT = tl
        assert self.srA <= al
        assert self.srF <= fl
        assert self.srT <= tl

        if not self.is_random:
            a_idx = np.linspace(0, al - 1, self.srA, dtype=int)
            f_idx = np.linspace(0, fl - 1, self.srF, dtype=int)
            t_idx = np.linspace(0, tl - 1, self.srT, dtype=int)
        else:
            a_idx = np.random.choice(al - 1, self.srA, replace=False)
            f_idx = np.random.choice(fl - 1, self.srF, replace=False)
            t_idx = np.random.choice(tl - 1, self.srT, replace=False)
            a_idx.sort()
            f_idx.sort()
            t_idx.sort()
        audio_s = audio[
            a_idx,
        ]
        face_s = face[
            f_idx,
        ]
        text_s = text[
            t_idx,
        ]  # 60x768 -> 10x768

        return audio_s, face_s, text_s, label


def load_impressionv2_dataset_all(
    srA=None,
    srF=None,
    srT=None,
    is_random=False,
    audio_emb: str = "lld",
    face_emb: str = "resnet18",
    text_emb: str = "bert",
) -> Tuple[List[th.utils.data.Dataset], List[str]]:
    """Loads 3 datasets containing embeddings for ImpressionV2 for a specific split. The dataset returns tensors of
    embeddings in the following order: audio, face, text, label. The label is not an embedding but the ground truth
    value. All three datasets will take up 13.3 GB space in the RAM.

    :return: train, valid and test datasets in the first list and the target names in the second list
    """
    train_ds, train_target_names = load_impressionv2_dataset_split(
        "train", audio_emb, face_emb, text_emb
    )
    valid_ds, test_target_names = load_impressionv2_dataset_split(
        "valid", audio_emb, face_emb, text_emb
    )
    test_ds, valid_target_names = load_impressionv2_dataset_split(
        "test", audio_emb, face_emb, text_emb
    )

    if (srA is not None) or (srF is not None) or (srT is not None):
        train_ds = TensorDatasetWithTransformer(
            train_ds, SamplerTransform(srA, srF, srT, is_random)
        )
        valid_ds = TensorDatasetWithTransformer(
            valid_ds, SamplerTransform(srA, srF, srT, False)
        )
        test_ds = TensorDatasetWithTransformer(
            test_ds, SamplerTransform(srA, srF, srT, False)
        )

    assert train_target_names == valid_target_names
    assert train_target_names == test_target_names
    return [train_ds, valid_ds, test_ds], train_target_names


def load_impressionv2_dataset_split(
    split: str, audio_emb: str, face_emb: str, text_emb: str
) -> Tuple[th.utils.data.Dataset, List[str]]:
    """Loads a dataset containing embeddings for ImpressionV2 for a specific split. The dataset returns tensors of
    embeddings in the following order: audio, face, text, label. The label is not an embedding but the ground truth
    value.

    :param split: Can be either "train", "valid" or "test".
    :return:
    """
    split_dir = IMPRESSIONV2_DIR / split
    gt, target_names = _get_gt(split)
    videos = sorted(gt.keys())
    video_dirs = [split_dir / video for video in videos]
    assert len(videos) == SET_SIZE[split]

    audio_embs = {"lld": _get_lld_audio}
    face_embs = {"resnet18": _get_resnet18_face}
    text_embs = {"bert": _get_bert_text}
    audio_norm = audio_embs[audio_emb](split, video_dirs)
    face_np = face_embs[face_emb](split, video_dirs)
    text_np = text_embs[text_emb](split, videos)

    audio_th = th.tensor(audio_norm)
    face_th = th.tensor(face_np)
    text_th = th.tensor(text_np)
    label_th = th.tensor([gt[video] for video in videos])
    return (
        th.utils.data.TensorDataset(audio_th, face_th, text_th, label_th),
        target_names,
    )


def _get_gt(split: str) -> Tuple[Dict, List[str]]:
    gt_file = IMPRESSIONV2_DIR / GT_NAMES[split]

    with open(gt_file, "rb") as f:
        gt_dict = pickle.load(f, encoding="latin1")
    target_names = list(gt_dict.keys())
    target_names.pop(-2)  # remove interview
    sample_names = sorted(list(gt_dict[target_names[0]].keys()))
    gt = {
        Path(sample_name).stem: [
            gt_dict[target_name][sample_name] for target_name in target_names
        ]
        for sample_name in sample_names
    }
    return gt, target_names


# region lld audio
def _get_lld_audio(split: str, video_dirs: List[Path]) -> np.ndarray:
    file = IMPRESSIONV2_DIR / f"{split}_audio.pkl"
    if not file.exists():
        audio_np = _create_lld_audio(video_dirs)
        audio_norm = _normalize_lld_audio(audio_np, file, split)
    else:
        with open(file, "rb") as f:
            gt_dict = pickle.load(f, encoding="latin1")
            audio_norm = gt_dict["audio_norm"]
    return audio_norm.astype(np.float32)


def _create_lld_audio(video_dirs: List[Path]) -> np.ndarray:
    audio_list = [
        pd.read_csv(video / "egemaps" / "lld.csv", sep=";") for video in video_dirs
    ]
    audio_list_pad = [
        np.pad(a.to_numpy(), [(0, 1526 - a.shape[0]), (0, 0)]) for a in audio_list
    ]
    audio_np = np.stack(audio_list_pad)
    return audio_np


def _normalize_lld_audio(audio_np: np.ndarray, file: Path, split: str) -> np.ndarray:
    if split == "train":
        mean = np.mean(audio_np, (0, 1))
        std = np.std(audio_np, (0, 1))
        audio_norm = (audio_np - mean) / std
        with open(file, "wb") as f:
            pickle.dump({"audio_norm": audio_norm, "mean": mean, "std": std}, f)
    else:
        with open(IMPRESSIONV2_DIR / f"train_audio.pkl", "rb") as f:
            gt_dict = pickle.load(f, encoding="latin1")
        mean = gt_dict["mean"]
        std = gt_dict["std"]
        audio_norm = (audio_np - mean) / std
        with open(file, "wb") as f:
            pickle.dump({"audio_norm": audio_norm}, f)
    return audio_norm


# endregion

# region bert text
def _get_bert_text(split: str, videos: List[str]) -> np.ndarray:
    file = IMPRESSIONV2_DIR / f"{split}_text.npy"
    if not file.exists():
        text_np = _create_bert_text(split, videos)
        np.save(file, text_np)
    else:
        text_np = np.load(file)
    return text_np


def _create_bert_text(split: str, videos: List[str]) -> np.ndarray:
    split_dir = EMBEDDING_DIR / "text" / split
    text_list = [np.load(split_dir / f"{video}_bertemd.npy") for video in videos]
    text_np = np.concatenate(text_list)
    return text_np


# endregion

# region resnet18 face
def _get_resnet18_face(split: str, video_dirs: List[Path]) -> np.ndarray:
    file = IMPRESSIONV2_DIR / f"{split}_face.npy"
    if not file.exists():
        face_np = _creat_resnet18_face(video_dirs)
        np.save(file, face_np)
    else:
        face_np = np.load(file)
    return face_np


def _creat_resnet18_face(video_dirs: List[Path]) -> np.ndarray:
    face_list = [
        np.load(video / "fi_face_resnet18" / "features.npy") for video in video_dirs
    ]
    face_list_pad = [np.pad(a, [(0, 459 - a.shape[0]), (0, 0)]) for a in face_list]
    face_np = np.stack(face_list_pad)
    return face_np


# endregion
