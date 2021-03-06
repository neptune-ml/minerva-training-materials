from math import ceil
from pathlib import Path

import imgaug as ia
import numpy as np
import pandas as pd
import torch
from imgaug import augmenters as iaa
from sklearn.externals import joblib
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import Dataset, DataLoader

from minerva.utils import decode_with_rescale
from .config import ALIGNER_AUXILARY_COLUMNS
from .utils import CropKeypoints, AlignKeypoints
from ..backend.base import BaseTransformer


class TargetEncoderPandas(BaseTransformer):
    def __init__(self, encode, no_encode):
        super().__init__()
        self.encode_cols = encode
        self.no_encode_cols = no_encode
        self.encoders_list = self._initialize_encoders()
        self.cols_with_encoders = [[col_name, encoder] for col_name, encoder in
                                   zip(self.encode_cols, self.encoders_list)]

    def _initialize_encoders(self):
        label_encoders = []
        for _ in range(len(self.encode_cols)):
            label_encoders.append(LabelEncoder())
        return label_encoders

    def fit(self, X, y, validation_data=None):
        # ToDo: no X here
        for col_name, encoder in self.cols_with_encoders:
            y_ = y[col_name].values.reshape(-1)
            encoder.fit(y=y_)
        return self

    def transform(self, X, y, validation_data=None):
        X_valid = None
        y_valid = None

        y_encoded = pd.DataFrame()
        y_valid_encoded = pd.DataFrame()

        if validation_data is not None:
            X_valid, y_valid = validation_data

        for col_name, encoder in self.cols_with_encoders:
            y_ = y[col_name].values.reshape(-1)
            y_encoded[col_name] = encoder.transform(y=y_)

            if validation_data is not None:
                y_valid_ = y_valid[col_name].values.reshape(-1)
                y_valid_encoded[col_name] = encoder.transform(y=y_valid_)

        for col_name in self.no_encode_cols:
            y_encoded[col_name] = y[col_name].values.reshape(-1)
            if validation_data is not None:
                y_valid_encoded[col_name] = y_valid[col_name].values.reshape(-1)

        if validation_data is not None:
            valid = (X_valid, y_valid_encoded)
        else:
            valid = None

        return {'X': X,
                'y': y_encoded,
                'validation_data': valid}

    def load(self, filepath):
        self.cols_with_encoders = joblib.load(filepath)
        return self

    def save(self, filepath):
        joblib.dump(self.cols_with_encoders, filepath)


class MetaDatasetBasic(Dataset):
    def __init__(self, X, y, img_dirpath, augmentation, target_size, bins_nr):
        super().__init__()
        self.img_dirpath = img_dirpath
        self.X = X.reset_index(drop=True)
        if y is not None:
            self.y = y.reset_index(drop=True)
        else:
            """
            Wouldn't work with kaggle submission Fix it
            """
            raise NotImplementedError('Not working with y being None')
        self.target_size = target_size
        self.bins_nr = bins_nr
        self.augmentation = augmentation
        self.preprocessing_function = None
        self.normalization_function = None

    def load_image(self, img_name, max_scaling_factor=8):
        img_path = Path(self.img_dirpath) / img_name
        return decode_with_rescale(img_path,
                                   minimum_shape=[d * 3 for d in self.target_size],
                                   max_scaling_factor=max_scaling_factor)

    def __len__(self):
        return self.X.shape[0]

    def __getitem__(self, index):
        raise NotImplementedError()


class DatasetLocalizer(MetaDatasetBasic):
    def __init__(self, X, y, img_dirpath, augmentation, target_size, bins_nr):
        super().__init__(X, y, img_dirpath, augmentation, target_size, bins_nr)
        self.preprocessing_function = localizer_preprocessing
        self.normalization_function = normalize_img

    def __getitem__(self, index):
        img_name = self.X['Image'].iloc[index]
        org_size = self.X[['height', 'width']].iloc[index].tolist()
        yi = self.y.iloc[index]

        Xi_img = self.load_image(img_name)
        Xi = np.asarray(Xi_img)

        Xi, yi = self.preprocessing_function(Xi, yi,
                                             self.augmentation,
                                             org_size=org_size,
                                             target_size=self.target_size,
                                             bins_nr=self.bins_nr)
        Xi = self.normalization_function(Xi)

        Xi_tensor = torch.from_numpy(Xi).permute(2, 0, 1).type(torch.FloatTensor)
        yi_tensors = torch.from_numpy(yi).type(torch.LongTensor)
        return Xi_tensor, yi_tensors


class DatasetAligner(MetaDatasetBasic):
    def __init__(self, X, y, crop_coordinates, img_dirpath, augmentation, target_size, bins_nr):
        super().__init__(X, y, img_dirpath, augmentation, target_size, bins_nr)
        self.crop_coordinates = crop_coordinates
        self.preprocessing_function = aligner_preprocessing
        self.normalization_function = normalize_img

    def __getitem__(self, index):
        img_name = self.X['Image'].iloc[index]
        org_size = self.X[['height', 'width']].iloc[index].tolist()
        crop_coordinates = self.crop_coordinates[index]
        yi = self.y.iloc[index]

        Xi_img = self.load_image(img_name)
        Xi = np.asarray(Xi_img)

        Xi, yi = self.preprocessing_function(Xi, yi,
                                             crop_coordinates,
                                             self.augmentation,
                                             org_size=org_size,
                                             target_size=self.target_size,
                                             bins_nr=self.bins_nr)
        Xi = self.normalization_function(Xi)

        Xi_tensor = torch.from_numpy(Xi).permute(2, 0, 1).type(torch.FloatTensor)
        yi_tensors = torch.from_numpy(yi).type(torch.LongTensor)
        return Xi_tensor, yi_tensors


class DatasetClassifier(MetaDatasetBasic):
    def __init__(self, X, y, aligner_coordinates, img_dirpath, augmentation, target_size, num_classes):
        super().__init__(X, y, img_dirpath, augmentation, target_size, num_classes)
        self.aligner_coordinates = aligner_coordinates
        self.preprocessing_function = classifier_preprocessing
        self.normalization_function = normalize_img
        self.num_classes = num_classes

    def __getitem__(self, index):
        img_name = self.X['Image'].iloc[index]
        org_shape = self.X[['height', 'width']].iloc[index].tolist()
        aligner_coordinates = self.aligner_coordinates[index]
        yi = self.y.iloc[index]

        Xi_img = self.load_image(img_name)
        Xi = np.asarray(Xi_img)

        Xi, yi = self.preprocessing_function(Xi, yi,
                                             aligner_coordinates,
                                             self.augmentation,
                                             org_size=org_shape,
                                             target_size=self.target_size)
        Xi = self.normalization_function(Xi)

        Xi_tensor = torch.from_numpy(Xi).permute(2, 0, 1)
        yi_tensor = torch.from_numpy(yi)
        return Xi_tensor.type(torch.FloatTensor), yi_tensor.type(torch.LongTensor)


class DataLoaderBasic(BaseTransformer):
    def __init__(self, dataset_params, loader_params):
        super().__init__()
        self.dataset_params = dataset_params
        self.loader_params = loader_params
        self._unpack_params()

    def _unpack_params(self):
        self.inference_dataset_params = self.dataset_params['inference']
        self.inference_loader_params = self.loader_params['inference']
        self.train_dataset_params = self.dataset_params['train']
        self.train_loader_params = self.loader_params['train']

    def transform(self, X, y, validation_data, train_mode):
        if train_mode:
            flow, steps = self.datagen_builder(X, y,
                                               self.train_dataset_params,
                                               self.train_loader_params)
        else:
            flow, steps = self.datagen_builder(X, y,
                                               self.inference_dataset_params,
                                               self.inference_loader_params)

        if validation_data is not None:
            X_valid, y_valid = validation_data
            valid_flow, valid_steps = self.datagen_builder(X_valid, y_valid,
                                                           self.inference_dataset_params,
                                                           self.inference_loader_params)
        else:
            valid_flow = None
            valid_steps = None

        return {'datagen': (flow, steps),
                'validation_datagen': (valid_flow, valid_steps)}

    def load(self, filepath):
        params = joblib.load(filepath)
        self.dataset_params = params['dataset_params']
        self.loader_params = params['loader_params']
        return self

    def save(self, filepath):
        params = {'dataset_params': self.dataset_params, 'loader_params': self.loader_params}
        joblib.dump(params, filepath)


class DataLoaderLocalizer(DataLoaderBasic):
    def __init__(self, dataset_params, loader_params):
        super().__init__(dataset_params, loader_params)
        self.dataset = DatasetLocalizer

    def datagen_builder(self, X, y, dataset_params, loader_params):
        dataset = self.dataset(X, y, **dataset_params)
        datagen = DataLoader(dataset, **loader_params)
        steps = ceil(X.shape[0] / loader_params['batch_size'])
        return datagen, steps


class DataLoaderAligner(DataLoaderBasic):
    def __init__(self, dataset_params, loader_params):
        super().__init__(dataset_params, loader_params)
        self.dataset = DatasetAligner

    def transform(self, X, y, crop_coordinates, validation_data, train_mode):

        if train_mode:
            flow, steps = self.datagen_builder(X, y, crop_coordinates,
                                               self.train_dataset_params,
                                               self.train_loader_params)
        else:
            flow, steps = self.datagen_builder(X, y, crop_coordinates,
                                               self.inference_dataset_params,
                                               self.inference_loader_params)

        if validation_data is not None:
            X_valid, y_valid, crop_coordinates_valid = validation_data
            valid_flow, valid_steps = self.datagen_builder(X_valid, y_valid, crop_coordinates_valid,
                                                           self.inference_dataset_params,
                                                           self.inference_loader_params)
        else:
            valid_flow = None
            valid_steps = None
        return {'datagen': (flow, steps),
                'validation_datagen': (valid_flow, valid_steps)}

    def datagen_builder(self, X, y, crop_coordinates, dataset_params, loader_params):
        dataset = self.dataset(X, y, crop_coordinates, **dataset_params)
        datagen = DataLoader(dataset, **loader_params)
        steps = ceil(X.shape[0] / loader_params['batch_size'])
        return datagen, steps


class DataLoaderClassifier(DataLoaderBasic):
    def __init__(self, dataset_params, loader_params):
        super().__init__(dataset_params, loader_params)
        self.dataset = DatasetClassifier

    def transform(self, X, y, align_coordinates, validation_data, train_mode):
        if train_mode:
            flow, steps = self.datagen_builder(X, y, align_coordinates,
                                               self.train_dataset_params,
                                               self.train_loader_params)
        else:
            flow, steps = self.datagen_builder(X, y, align_coordinates,
                                               self.inference_dataset_params,
                                               self.inference_loader_params)

        if validation_data is not None:
            X_valid, y_valid, align_coordinates_valid = validation_data
            valid_flow, valid_steps = self.datagen_builder(X_valid, y_valid, align_coordinates_valid,
                                                           self.inference_dataset_params,
                                                           self.inference_loader_params)
        else:
            valid_flow = None
            valid_steps = None

        return {'datagen': (flow, steps),
                'validation_datagen': (valid_flow, valid_steps)}

    def datagen_builder(self, X, y, align_coordinates, dataset_params, loader_params):
        dataset = self.dataset(X, y, align_coordinates, **dataset_params)
        datagen = DataLoader(dataset, **loader_params)
        steps = ceil(X.shape[0] / loader_params['batch_size'])
        return datagen, steps


def localizer_preprocessing(img, target, augmentation, *, org_size, target_size, bins_nr):
    final_height, finale_width = target_size
    img_height, img_width = img.shape[:-1]

    load_scale = iaa.Scale({"height": img_height, "width": img_width}).to_deterministic()
    final_scale = iaa.Scale({"height": final_height, "width": finale_width}).to_deterministic()
    augmenter = iaa.Affine(rotate=(-10, 10), scale=(1 / 1.2, 1.2)).to_deterministic()

    if augmentation:
        transformations = [load_scale, augmenter, final_scale]
    else:
        transformations = [final_scale]
    transformer = iaa.Sequential(transformations).to_deterministic()

    aug_X = transformer.augment_image(img)

    keypoints = ia.KeypointsOnImage([
        ia.Keypoint(x=int(target.bbox1_x), y=int(target.bbox1_y)),
        ia.Keypoint(x=int(target.bbox2_x), y=int(target.bbox2_y))],
        shape=org_size)
    aug_points = transformer.augment_keypoints([keypoints])[0]
    aug_points_formatted = np.reshape(aug_points.get_coords_array(), -1).astype(np.float)
    aug_points_binned = bin_quantizer(aug_points_formatted, target_size, bins_nr)

    return aug_X, aug_points_binned


def aligner_preprocessing(img, target, crop_coordinates, augmentation, *, org_size, target_size, bins_nr):
    img_height, img_width = img.shape[:-1]
    final_height, final_width = target_size

    keypoints = ia.KeypointsOnImage([ia.Keypoint(x=int(target.bonnet_x), y=int(target.bonnet_y)),
                                     ia.Keypoint(x=int(target.blowhead_x), y=int(target.blowhead_y))],
                                    shape=org_size)
    crop_coordinates = ia.KeypointsOnImage([ia.Keypoint(x=crop_coordinates[0], y=crop_coordinates[1]),
                                            ia.Keypoint(x=crop_coordinates[2], y=crop_coordinates[3])],
                                           shape=org_size)
    # scale crop coordinates to current image shape
    load_scale = iaa.Scale({'height': img_height, 'width': img_width}, deterministic=True)
    crop_coordinates = load_scale.augment_keypoints([crop_coordinates])[0]
    crop_coordinates = crop_coordinates.get_coords_array().flatten()

    crop = CropKeypoints(crop_coordinates).to_deterministic()
    augmenter = iaa.Sequential([iaa.Fliplr(0.5),
                                iaa.Flipud(0.5),
                                iaa.Affine(
                                    translate_px={"x": (-4, 4), "y": (-4, 4)},
                                    rotate=(-180, 180),
                                    scale=(1.0, 1.5)),
                                ]).to_deterministic()
    final_scale = iaa.Scale({"height": final_height, "width": final_width}).to_deterministic()

    if augmentation:
        transformations = [load_scale, crop, augmenter, final_scale]
    else:
        transformations = [load_scale, crop, final_scale]

    transformer = iaa.Sequential(transformations).to_deterministic()

    aug_X = transformer.augment_image(img)
    aug_points = transformer.augment_keypoints([keypoints])[0]

    # bin key-points
    aug_points_formatted = np.reshape(aug_points.get_coords_array(), -1).astype(np.float)
    aug_points_binned = bin_quantizer(aug_points_formatted, target_size, bins_nr)
    aug_target_binned = np.hstack([aug_points_binned, target[ALIGNER_AUXILARY_COLUMNS].values])
    return aug_X, aug_target_binned


def classifier_preprocessing(img, target, aligner_coordinates, augmentation, *, org_size, target_size):
    img_height, img_width = img.shape[:-1]
    final_height, final_width = target_size

    aligner_coordinates = ia.KeypointsOnImage([ia.Keypoint(x=aligner_coordinates[0], y=aligner_coordinates[1]),
                                               ia.Keypoint(x=aligner_coordinates[2], y=aligner_coordinates[3])],
                                              shape=org_size)
    # scale aligner coordinates to current image shape
    load_scale = iaa.Scale({'height': img_height, 'width': img_width}, deterministic=True)
    aligner_coordinates = load_scale.augment_keypoints([aligner_coordinates])[0]
    aligner_coordinates = aligner_coordinates.get_coords_array().flatten()

    align = AlignKeypoints(aligner_coordinates, target_size).to_deterministic()
    augmenter = iaa.Sequential([iaa.Affine(translate_px={"x": (-4, 4), "y": (-4, 4)},
                                           rotate=(-4, 4),
                                           scale=(1.0, 1.3)),
                                iaa.Flipud(0.5),
                                iaa.Fliplr(0.5),
                                ]).to_deterministic()
    final_scale = iaa.Scale({"height": final_height, "width": final_width}).to_deterministic()

    if augmentation:
        transformations = [align, augmenter, final_scale]
    else:
        transformations = [align, final_scale]

    transformer = iaa.Sequential(transformations).to_deterministic()

    aug_X = transformer.augment_image(img)
    target_np = target.values.astype(np.int64)
    return aug_X, target_np


def normalize_img(img):
    mean = [0.28201905, 0.37246801, 0.42341868]
    std = [0.13609867, 0.12380088, 0.13325344]
    img_ = img.astype(np.float64) / 255.
    img_ = (img_ - mean) / std
    return img_


def denormalize_img(img):
    mean = [0.28201905, 0.37246801, 0.42341868]
    std = [0.13609867, 0.12380088, 0.13325344]
    img_ = (img * std) + mean
    return img_


def bin_quantizer(coordinates, shape, bins_nr):
    height, width = shape
    bins_nr_ = bins_nr - 1  # hack/adjustment when value is at the right edge

    x_coordinates = coordinates[[1, 3]]
    bins_x = [i * (width / bins_nr_) for i in range(bins_nr_)]
    binned_x = np.digitize(x_coordinates, bins_x)

    y_coordinates = coordinates[[0, 2]]
    bins_y = [i * (height / bins_nr_) for i in range(bins_nr_)]
    binned_y = np.digitize(y_coordinates, bins_y)

    binned_coordinates = np.array([binned_y[0], binned_x[0], binned_y[1], binned_x[1]])
    return binned_coordinates
