import os
import numpy as np
import matplotlib.pyplot as plt
import wandb
from tqdm import tqdm
from abc import ABC, abstractmethod
import torch
from torch.utils.data import DataLoader, TensorDataset, random_split
import torch.nn as nn
import torch.optim as optim
import seaborn as sns
import pandas as pd
from sklearn.preprocessing import LabelEncoder
from sklearn.preprocessing import StandardScaler
# from imblearn.over_sampling import SMOTE


class BaseDataset(ABC):
    def __init__(self):
        self.dataset = None

    @abstractmethod
    def generate_dataset(self):
        pass

    def get_dataloader(self, batch_size=64, shuffle=True, with_labels=False):
        """Generate a dataloader for the dataset."""
        if self.dataset is None:
            self.generate_dataset()

        if with_labels:
            tensor_dataset = TensorDataset(self.dataset['x'], self.dataset['y'])
        else:
            tensor_dataset = TensorDataset(self.dataset['x'])

        return DataLoader(tensor_dataset, batch_size=batch_size, shuffle=shuffle)

    def get_dataset_shape(self):
        assert self.dataset is not None, 'Dataset not generated'
        return self.dataset['x'].shape

    @abstractmethod
    def get_features_with_mask(self):
        pass


class CategoricalEncoder:
    """Class for encoding and decoding categorical values in a DataFrame."""
    def __init__(self, data):
        self.structure = []
        self.map_categories_to_indices = {}
        self.map_indices_to_categories = {}
        self.columns_names = []
        self.data = data
        
        self._encode_dataset()

    def _encode_dataset(self):
        """Encode a DataFrame of categorical values."""
        for i, column in enumerate(self.data.columns):
            self.columns_names.append(column)
            unique_values = self.data[column].unique().tolist()
            self.structure.append(len(unique_values))
            unique_values.sort()
            indices_encoding = {val: idx for idx, val in enumerate(unique_values)}
            self.map_categories_to_indices[i] = indices_encoding
            self.map_indices_to_categories[i] = {idx: val for val, idx in indices_encoding.items()}

    def _decode_index(self, i, index):
        """Decode an individual index back to its value."""
        return self.map_indices_to_categories[i][index]    

    def indices_to_dataframe(self, indices):
        """Decode a 2D array of numerical labels to a DataFrame with the original values."""
        decoded_values = self.decode_indices(indices)
        decoded_df = pd.DataFrame(decoded_values, columns=self.columns_names)
        return decoded_df
    
    def encoded_data(self):
        """Return the encoded data."""
        encoded_data = self.data.copy()
        for i, column in enumerate(self.data.columns):
            encoded_data[column] = self.data[column].map(self.map_categories_to_indices[i])
        return encoded_data.to_numpy()


class RealDataset(BaseDataset):
    """Class for generating a dataset to perform anomaly correction."""
    def __init__(self, csv_filename_in_datasets = None):
        super().__init__()
        
        self.row_x_indices_np = None
        self.row_y_np = None
        self.categorical_encoder = None
        self.proba = None
        
        self._get_dataset(csv_filename_in_datasets)
        
    def _get_dataset(self, csv_filename_in_datasets, path='../datasets/'):
        # Read the dataframe
        dataframe = pd.read_csv(path + csv_filename_in_datasets + '.csv')
        
        # Split the data into features and target
        x = dataframe.drop(columns=['y'])
        y = dataframe['y'].to_numpy()
        
        encoder = LabelEncoder()
        encoder.fit(y)
        y = encoder.transform(y)
        self.row_y_np = y.astype(np.bool)
        
        # Extract the structure of the data with a categorical encoder
        self.categorical_encoder = CategoricalEncoder(x)
        structure = self.categorical_encoder.structure
        
        # Transform the data to probabilities representation
        self.proba = Probabilities(structure)
        self.row_x_indices_np = self.categorical_encoder.encoded_data()
    
    def get_degrees_of_freedom_categories(self):
        """Return the sum of number of values for each category."""
        return sum(self.proba.structure)
    
    def get_classifier_dataloader(self, training_prop=0.7, batch_size=64, shuffle=True):
        """Generate a dataloader for the dataset."""
        dataset = self.generate_dataset(indices=True)
        
        x, y = dataset['x'], dataset['y']
        
        # # # Preprocess data
        # scaler = StandardScaler()
        # x = scaler.fit_transform(x.numpy())
        # x = torch.tensor(x, dtype=torch.float64)
        
        # Create a TensorDataset from x and y
        tensor_dataset = TensorDataset(x, y)
        
        # Calculate train and validation sizes
        train_size = int(training_prop * len(tensor_dataset))
        val_size = len(tensor_dataset) - train_size
        
        # Split the dataset into training and validation sets
        train_dataset, _ = random_split(tensor_dataset, [train_size, val_size])
        
        return DataLoader(train_dataset, batch_size=batch_size, shuffle=shuffle)
    
    def generate_dataset(self, remove_anomalies=False, only_anomalies=False, indices=False, eps=1e-6):
        """
        Generate the dataset for the model starting from a dataframe.
        """
        assert not (remove_anomalies and only_anomalies), 'Cannot remove and keep only anomalies at the same time'
        
        x_indices = torch.tensor(self.row_x_indices_np, dtype=torch.float64)
        y = self.row_y_np
        
        if not (remove_anomalies or only_anomalies) and indices:
            x = x_indices
            y = np.expand_dims(y, axis=1)
            y = torch.tensor(y, dtype=torch.float64) 
            return {'x': x, 'y': y}
        
        x = self.proba.to_onehot(self.row_x_indices_np)
        
        # Remove anomalies if needed for DDPM training
        if remove_anomalies:
            print('Removing anomalies')
            x = x[~y]
            x_indices = x_indices[~y]
            y = y[~y]
            
        # Get only anomalies if needed for anomaly detection
        elif only_anomalies:
            x = x[y]
            x_indices = x_indices[y]
            y = y[y]

        y = np.expand_dims(y, axis=1)

        # convert to torch tensors
        x = torch.tensor(x, dtype=torch.float64)
        y = torch.tensor(y, dtype=torch.bool)

        x = torch.log((x + eps) / (1 - x + eps))

        self.dataset = {'x': x, 'y': y, 'indices': x_indices}

        return self.dataset

    def logits_to_values(self, logits):
        """Convert the logits to values."""
        if isinstance(logits, torch.Tensor):
            logits = logits.cpu().numpy()
        return self.proba.logits_to_values(logits)
    
    def get_features_with_mask(self):
        """Generate the dataset with the mask to inpaint."""

        dataset = self.dataset if self.dataset is not None else self.generate_dataset()

        # add the mask to the dataset
        tmp = dataset['y'].to(torch.bool) 
        del dataset['y']
        dataset['mask'] = tmp
        return dataset

    def modified_dataset(self, transformed_data):
        """Returns the transformed data in the original format."""
        transformed_data = self.logit_to_values(transformed_data)
        
        return self.categorical_encoder.indices_to_dataframe(transformed_data)
    
    
class SumCategoricalDataset(BaseDataset):
    """Class to generate a toy dataset for anomaly correction."""
    
    def __init__(self, size=None, structure=None, threshold=None):
        super().__init__()
        
        self.threshold = threshold
        
        self.probabilities = None
        self.row_x_indices_np = None
        self.row_y_np = None
        self.proba = None
        
        if size and structure and threshold is not None:
            self._generate_dataset(size, structure)
        
    def _generate_dataset(self, size, structure):
        """Generate the dataset."""
        
        # Instantiate the probabilities object
        self.proba = Probabilities(structure)
        
        # Generate raw data
        p = np.random.random(size=(size, sum(structure)))
        self.probabilities = self.proba.normalize(p)

        # Convert probabilities to onehot encoding
        x = self.proba.prob_to_onehot(p)
        
        self.row_x_indices_np = self.proba.onehot_to_values(x)
        # Generate labels based on the threshold
        self.row_y_np = np.sum(self.row_x_indices_np, axis=1) > self.threshold
    
    def get_classifier_dataloader(self, training_prop=0.7, batch_size=64, shuffle=True):
        """Generate a dataloader for the dataset."""
        dataset = self.generate_dataset(indices=True)
        
        x, y = dataset['x'], dataset['y']
        
        # Create a TensorDataset from x and y
        tensor_dataset = TensorDataset(x, y)
        
        # Calculate train and validation sizes
        train_size = int(training_prop * len(tensor_dataset))
        val_size = len(tensor_dataset) - train_size
        
        # Split the dataset into training and validation sets
        train_dataset, _ = random_split(tensor_dataset, [train_size, val_size])
        
        return DataLoader(train_dataset, batch_size=batch_size, shuffle=shuffle)

    def generate_dataset(self, remove_anomalies=False, only_anomalies=False, indices=False, eps=1e-6):
        """
        Generate a dataset in probability space that represents arrays of label encoded categories.
        The y labels are binary, True/Anomaly if the sum of the values in the array exceeds the threshold.
        """
        
        assert not (remove_anomalies and only_anomalies), 'Cannot remove and keep only anomalies at the same time'
        
        x_indices = torch.tensor(self.row_x_indices_np, dtype=torch.float64)
        y = self.row_y_np
        if not (remove_anomalies or only_anomalies) and indices:
            x = x_indices
            y = np.expand_dims(y, axis=1)
            y = torch.tensor(y, dtype=torch.float64) 
            return {'x': x, 'y': y}
        
        x = self.probabilities
        
        if remove_anomalies:
            x = x[~y]
            x_indices = x_indices[~y]
            y = y[~y]
        elif only_anomalies:
            x = x[y]
            x_indices = x_indices[y]
            y = y[y]
        
        y = np.expand_dims(y, axis=1)
        x = torch.tensor(x, dtype=torch.float64)
        y = torch.tensor(y, dtype=torch.float64)
        
        x = torch.log((x + eps) / (1 - x + eps))
        
        self.dataset = {'x': x, 'y': y, 'indices': x_indices}
        
        return self.dataset
    
    def logits_to_values(self, logits):
        """Convert the logits to values."""
        if isinstance(logits, torch.Tensor):
            logits = logits.cpu().numpy()
        return self.proba.logits_to_values(logits)

    def get_features_with_mask(self, mask_anomaly_points=False, mask_one_feature=True, label_values_mask=False, eps=1e-6):
        """Generate the dataset with the mask to inpaint."""
        
        dataset = self.dataset if self.dataset is not None else self.generate_dataset(eps=eps)
        masks = []

        if mask_anomaly_points:
            tmp = self._mask_anomaly_points()
            masks.append(tmp)
            masks.append(tmp.numpy() if label_values_mask else None)
        elif mask_one_feature:
            masks = self._mask_one_feature_values(label_values_mask)
        else:
            masks = self._mask_features_values(label_values_mask)
        mask = masks[0]   
        values_mask = masks[1] if label_values_mask else None

        dataset['mask'] = mask
        dataset['values_mask'] = values_mask
        
        return dataset

    def _mask_anomaly_points(self):
        """Identify the anomaly points in the dataset."""
        
        dataset = self.dataset if self.dataset is not None else self.generate_dataset()
        mask = dataset['y']
        
        return mask.to(torch.bool)

    def _mask_one_feature_values(self, label_values_mask=False):
        """
        Identify which element(s) in the label values 2D array contribute the most to each row's sum exceeding a
        certain threshold.
        If there are multiple elements contributing equally to the sum, the one with the lowest index is selected.
        Since we work with probability space coming from a one-hot encoding, the mask is repeated for each feature.
        
        Example in terms of the label values:
        self.label_values = np.array([[0 2 3], [1 2 2]])
        self.threshold = 4
        
        Output:
        > array([[False, False,  True],
                [False, True,  False]])

        The mask for the probabilities is:
        > array([[False, False,  True, False, False,  True, False,  True],
                [False, True,  False, False, True,  False, False,  True]])
        """

        # Fetch the label values
        array = self.dataset['indices'].numpy()

        # Calculate the sum of each row
        row_sums = np.sum(array, axis=1)

        # Create a boolean array to store the result
        result = np.zeros_like(array, dtype=bool)

        # Identify rows where the sum exceeds the threshold
        exceeding_rows_indices = np.where(row_sums > self.threshold)[0]

        if exceeding_rows_indices.size > 0:
            # Find the index of the maximum value in the rows that exceed the threshold
            max_value_indices = np.argmax(array[exceeding_rows_indices], axis=1)
            result[exceeding_rows_indices, max_value_indices] = True

        # Repeat each column according to the specified repetition counts
        repeated_result = np.repeat(result, self.proba.structure, axis=1)
        repeated_result = torch.tensor(repeated_result, dtype=torch.bool)

        if label_values_mask:
            return [repeated_result, result]
        return [repeated_result]

    def _mask_features_values(self, label_values_mask=False):
        """
        Identify which elements in the array contribute the most to each row's sum exceeding a certain threshold.
        If there are multiple elements contributing equally to the sum then both are selected.
        Since we work with probability space coming from a one-hot encoding, the mask is repeated for each feature.
        
        Example in terms of the label values:
        self.label_values = np.array([[0 2 3], [1 2 2]])
        self.threshold = 4
        
        Output:
        > array([[False, False,  True],
                [False, True,  True]])
        """

        # Fetch the label values 
        array = self.dataset['indices'].numpy()

        # Calculate the sum of each row
        row_sums = np.sum(array, axis=1)

        # Identify rows where the sum exceeds the threshold
        exceed_threshold = row_sums > self.threshold

        # Identify the maximum value in each row
        row_maxes = np.max(array, axis=1)

        # For each row, create a boolean array where True indicates that the element is the maximum
        is_max = array == row_maxes[:, None]

        # Combine the conditions: the sum exceeds the threshold and the element is the maximum
        result = np.logical_and(exceed_threshold[:, None], is_max)

        repeated_result = np.repeat(result, self.proba.structure, axis=1)

        repeated_result = torch.tensor(repeated_result, dtype=torch.bool)

        if label_values_mask:
            return [repeated_result, result]
        return [repeated_result]


class GaussianDataset(BaseDataset):
    """
    Author: Luis
    Class to generate the dataset for the DDPM model.
    """

    def __init__(self):
        super().__init__()

    def _generate_samples(self, mean, cov, num_samples):
        """
        Generates samples using an alternative approach to handle non-positive definite covariance matrices.
        """
        mean_tensor = torch.tensor(mean, dtype=torch.float64)
        cov_tensor = torch.tensor(cov, dtype=torch.float64)

        # Ensure the covariance matrix is symmetric
        cov_tensor = (cov_tensor + cov_tensor.T) / 2

        # Use SVD to generate samples
        U, S, V = torch.svd(cov_tensor)
        transform_matrix = U @ torch.diag(torch.sqrt(S))

        normal_samples = torch.randn(num_samples, len(mean), dtype=torch.float64)
        samples = normal_samples @ transform_matrix.T + mean_tensor

        return samples

    def generate_dataset(self, means, covariances, num_samples_per_distribution, labels=None):
        """
        Generates a dataset based on the provided Gaussian distribution parameters.
        
        Parameters:
        - means (list): List of mean vectors for each Gaussian distribution.
        - covariances (list): List of covariance matrices for each Gaussian distribution.
        - num_samples_per_distribution (list): List of sample counts for each Gaussian distribution.
        - labels (list): List of labels corresponding to each distribution.
        
        Returns:
        - dict: Dictionary containing the dataset ('x') and the labels ('y') as torch tensors.

        # todo: signature does not match the one in the abstract class
        """

        if labels is None:
            labels = torch.ones(len(means), dtype=torch.int)  # Generate default labels

        assert len(means) == len(covariances) == len(num_samples_per_distribution) == len(labels), \
            "The lengths of means, covariances, num_samples_per_distribution, and labels must be the same."

        samples = []
        labels_list = []

        for mean, cov, num_samples, label in zip(means, covariances, num_samples_per_distribution, labels):
            samples_i = self._generate_samples(mean, cov, num_samples)
            labels_i = torch.full((num_samples, 1), label)
            samples.append(samples_i)
            labels_list.append(labels_i)

        # Concatenate all samples and labels
        x_tensor = torch.cat(samples, dim=0)
        y_tensor = torch.cat(labels_list, dim=0)

        self.dataset = {'x': x_tensor, 'y': y_tensor}

        return self.dataset

    def get_features_with_mask(self, means, covariances, num_samples_per_distribution, boolean_labels):
        """
        Generates the dataset with the mask to inpaint.

        # todo: signature does not match the one in the abstract class
        """

        # inspect if the labels are boolean
        assert all(isinstance(label, bool) for label in boolean_labels), 'Labels must be boolean'

        dataset = self.generate_dataset(means, covariances, num_samples_per_distribution, boolean_labels).copy()
        dataset['mask'] = dataset.pop('y')
        dataset['mask'] = dataset['mask'].to(torch.bool)

        return dataset

    def plot_data(self, filename, save_locally=False, save_wandb=False, path="../plots/"):
        """
        Plots the dataset with different colors for different labels.
        """
        assert self.dataset is not None, 'Dataset not generated'
        
        if not save_locally and not save_wandb:
            return

        if not os.path.exists(path) and save_locally:
            os.makedirs(path)

        x = self.dataset['x'].numpy()
        y = self.dataset['y'].numpy().flatten()

        unique_labels = np.unique(y)
        plt.figure(figsize=(8, 6))

        for label in unique_labels:
            mask = y == label
            plt.scatter(x[mask, 0], x[mask, 1], alpha=0.5, label=f'Labels {int(label)}')

        plt.title('2D Gaussians')
        plt.xlabel('X')
        plt.ylabel('Y')
        plt.legend()
        plt.grid(True)

        if save_wandb:
            wandb.log({filename: wandb.Image(plt)})
        if save_locally:
            plt.savefig(path + filename + '.png')

def plot_generated_samples(samples, filename, save_locally=False, save_wandb=False, path="../plots/"):
    """ Author: Luis
    Save the plot of the generated samples in the plots folder and in the wandb dashboard.
    """
    if not os.path.exists(path) and save_locally:
        os.makedirs(path)

    fig = plt.figure()
    if len(samples) == 1:
        x = samples[0]
    else:
        x = samples  # plot inpainting samples
    if len(samples) == 2:
        y = samples[1]
        mask = y == 1
        plt.scatter(x[~mask, 0], x[~mask, 1], alpha=0.5, label='Normal')
        plt.scatter(x[mask, 0], x[mask, 1], alpha=0.5, label='Anomaly')
        plt.legend()
    else:
        plt.scatter(x[:, 0], x[:, 1], alpha=0.5)
    plt.title(filename)
    plt.xlabel('X')
    plt.ylabel('Y')

    if save_locally:
        plt.savefig(path + filename + '.png')
    if save_wandb:
        wandb.log({filename: wandb.Image(fig)})


def plot_data_to_inpaint(x, mask, filename=None, save_locally=False, save_wandb=False, path="../plots/"):
    """ Author: Luis
    Plot the dataset to inpaint with the mask applied.
    It saves the plot in the wandb dashboard.
    """
    assert filename is not None, 'Filename must be provided'
    if not save_locally and not save_wandb:
        return
    
    # Convert tensors to numpy arrays for plotting
    x = x.numpy()
    mask = mask.numpy().squeeze()

    # Scatter plot of the dataset
    fig = plt.figure(figsize=(8, 6))
    plt.scatter(x[~mask, 0], x[~mask, 1], alpha=0.5, label='Reference')
    plt.scatter(x[mask, 0], x[mask, 1], alpha=0.5, label='Masked')
    plt.title('Dataset with Mask')
    plt.xlabel('X')
    plt.ylabel('Y')
    plt.legend()

    if save_locally:
        if not os.path.exists(path):
            os.makedirs(path)
        plt.savefig(path + filename + '.png')
    
    if save_wandb:
        wandb.log({filename: wandb.Image(fig)})


class EMA:
    """
    Exponential Moving Average
    This is a way to impose a smoother training process
    The weights of the model do not change abruptly
    """

    def __init__(self, beta):
        super().__init__()
        self.beta = beta
        assert beta < 1 and beta > 0, 'The beta EMA must in the range (0, 1)'
        self.step = 0

    def update_model_average(self, ma_model, current_model):
        """updates the parameters of the model average"""
        for current_params, ma_params in zip(current_model.parameters(), ma_model.parameters()):
            old_weight, up_weight = ma_params.data, current_params.data
            ma_params.data = self.update_average(old_weight, up_weight)

    def update_average(self, old, new):
        """
        core idea of EMA
        the weights are an interpolation between the old and new weights weighted by beta
        """
        if old is None:
            return new
        return old * self.beta + (1 - self.beta) * new

    def step_ema(self, ema_model, model, step_start_ema=2000):
        # warmup phase
        if self.step < step_start_ema:
            EMA.reset_parameters(ema_model, model)
            self.step += 1
            return
        # update the model average
        self.update_model_average(ema_model, model)
        self.step += 1

    @staticmethod
    def reset_parameters(ema_model, model):
        """
        Resets the parameters of the EMA model to match those of the current model.
        """
        ema_model.load_state_dict(model.state_dict())


def plot_loss(losses, filename, save_locally=False, save_wandb=False, path="../plots/"):
    """plot the loss and save it in the plots folder and in the wandb dashboard."""
    if not os.path.exists(path):
        os.makedirs(path)

    fig = plt.figure()
    plt.plot(losses)
    plt.title('Training Loss')
    plt.xlabel('Epoch')
    plt.ylabel('Loss')

    if save_locally:
        plt.savefig(path + filename + '.png')

    if save_wandb:
        wandb.log({filename: wandb.Image(fig)})


class BaseNoiseScheduler(ABC):
    """ Author: Luis
    Base class for the noise scheduler in the diffusion model.
    It is an abstract class that defines the methods that the noise scheduler should implement.
    """

    def __init__(self, noise_time_steps, dataset_shape):
        self.noise_time_steps = noise_time_steps
        num_dims_to_add = len(dataset_shape) - 1
        self.num_dims_to_add = num_dims_to_add
        self.betas = None
        self.alphas = None
        self.alpha_bar = None
        self.sqrt_alpha_bar = None
        self.sqrt_one_minus_alpha_bar = None

    @abstractmethod
    def _initialize_schedule(self):
        pass

    def send_to_device(self, device):
        """
        Send the scheduler parameters to the device for efficient computation.
        """
        self.betas = self.betas.to(device)
        self.alphas = self.alphas.to(device)
        self.alpha_bar = self.alpha_bar.to(device)
        self.sqrt_alpha_bar = self.sqrt_alpha_bar.to(device)
        self.sqrt_one_minus_alpha_bar = self.sqrt_one_minus_alpha_bar.to(device)

    def add_noise(self, x0, noise, t):
        r"""
        Forward method for diffusion
        x_{t} = \sqrt{\alpha_bar_{t}}x_{0} + \sqrt{1-\alpha_bar_{t}}\epsilon
        x_{0} has shape (batch_size, ...)
        noise has shape (batch_size, ...)
        t has shape (batch_size,)
        The scheduler parameters already have the correct shape to match x_{0} and noise.
        """
        return self.sqrt_alpha_bar[t] * x0 + self.sqrt_one_minus_alpha_bar[t] * noise

    def sample_prev_step(self, x_t, predicted_noise, t):
        r"""
        Reverse sampling method for diffusion
        x_{t-1} ~ p_{\theta}(x_{t-1}|x_{t})
        """

        # noise = z ~ N(0, I) if t > 1 else 0
        backward_noise = torch.randn_like(x_t) if t[0] > 0 else torch.zeros_like(x_t)

        mean = x_t - (self.betas[t] * predicted_noise) / self.sqrt_one_minus_alpha_bar[t]
        mean = mean / torch.sqrt(self.alphas[t])
        std = (1.0 - self.alpha_bar[t - 1]) / (1.0 - self.alpha_bar[t]) * self.betas[t]

        # x_{t-1} = predicted_mean_reconstruction + fixed_std * noise
        return mean + std * backward_noise

    def sample_current_state_inpainting(self, x_t_minus_one, t):
        """
        Resampling method for inpainting
        """

        # noise = z ~ N(0, I)
        noise = torch.randn_like(x_t_minus_one)

        return x_t_minus_one * torch.sqrt(self.alphas[t]) + torch.sqrt(self.betas[t]) * noise
        # return x_t_minus_one * torch.sqrt(self.alphas[t - 1]) + torch.sqrt(self.betas[t - 1]) * noise


class LinearNoiseScheduler(BaseNoiseScheduler):
    r""" Author: Luis
    Class for the linear noise scheduler that is used in DDPM.
    The dimensions of the noise scheduler parameters are expanded to match the
    dimensions of the samples of the dataset. 
    This is required to make broadcasting operations between the noise and the samples.
    This change is only added to the betas attribute and is propagated to the other attributes.
    """

    def __init__(self, noise_time_steps, dataset_shape, beta_start=1e-4, beta_end=2e-2):
        super().__init__(noise_time_steps, dataset_shape)
        self.beta_start = beta_start
        self.beta_end = beta_end
        self._initialize_schedule()

    def _initialize_schedule(self):
        linspace = torch.linspace(self.beta_start, self.beta_end, self.noise_time_steps)  # note: Omar split this line
        self.betas = linspace.view(*([-1] + [1] * self.num_dims_to_add))  # because it was too long
        self.alphas = 1. - self.betas
        self.alpha_bar = torch.cumprod(self.alphas, dim=0)
        self.sqrt_alpha_bar = torch.sqrt(self.alpha_bar)
        self.sqrt_one_minus_alpha_bar = torch.sqrt(1 - self.alpha_bar)


class CosineNoiseScheduler(BaseNoiseScheduler):
    """
    Author: Luis
    Cosine Noise Scheduler for DDPM model.
    # todo: needs improvement with offset parameter. Check papers for more details.
    """

    def __init__(self, noise_time_steps: int, dataset_shape: tuple = None, s: float = 0.008):
        super().__init__(noise_time_steps, dataset_shape)
        self.s = torch.tensor(s, dtype=torch.float64)
        self._initialize_schedule()

    def _cosine_schedule(self, t: torch.tensor) -> torch.tensor:
        """
        Computes the cosine schedule function.
        """
        return torch.cos((t / self.noise_time_steps + self.s) / (1 + self.s) * torch.pi / 2) ** 2

    def _initialize_schedule(self):
        """
        Initializes the schedule for alpha and beta values based on the cosine schedule.
        """
        t = torch.linspace(0, self.noise_time_steps, self.noise_time_steps, dtype=torch.float64)
        self.alpha_bar = self._cosine_schedule(t) / self._cosine_schedule(torch.tensor(0.0, dtype=torch.float64))

        self.alphas = torch.ones_like(self.alpha_bar)
        self.alphas[1:] = self.alpha_bar[1:] / self.alpha_bar[:-1]
        self.alphas[0] = self.alpha_bar[0]

        self.betas = torch.clamp(1 - self.alphas, 0.0001, 0.999)

        shape = [-1] + [1] * self.num_dims_to_add
        self.betas = self.betas.view(*shape)
        self.alphas = self.alphas.view(*shape)
        self.alpha_bar = self.alpha_bar.view(*shape)
        self.sqrt_alpha_bar = torch.sqrt(self.alpha_bar)
        self.sqrt_one_minus_alpha_bar = torch.sqrt(1 - self.alpha_bar)


class Probabilities:
    """ Author: Omar
    This class helps normalize probabilities for a set
    of features with different number of values
    """

    def __init__(self, structure: list | tuple, dtype=np.float64):  # todo rename n_values -> structure
        self.structure = structure
        self.n = len(structure)
        self.length = sum(structure)
        self.dtype = dtype
        self.mat = None
        self._set_mat()

    def _set_mat(self):
        """Create binary masks that divide the various features"""
        self.mat = np.zeros((self.length, self.length), dtype=self.dtype)
        for i in range(self.n):
            start = sum(self.structure[:i])
            for j in range(self.structure[i]):
                self.mat[start:start + j + 1, start:start + self.structure[i]] = 1

    def normalize(self, p: np.array):
        """Cap at 0, then normalize the probabilities for each feature"""
        assert len(p.shape) == 2, f'{len(p.shape)} != 2'
        assert p.shape[1] == self.length, f'{p.shape[1]} != {self.length}'
        p = np.maximum(0, p)
        s = np.dot(p, self.mat)
        assert np.all(s > 0), f'Zero sum: p={p}, s={s}'
        return p / s

    def to_onehot(self, x: np.array):
        """Convert the original values to one-hot encoding"""
        assert len(x.shape) == 2, f'{len(x.shape)} != 2'
        assert x.shape[1] == self.n, f'{x.shape[1]} != {self.n}'
        # check that each value of x is less than the number of values for that feature
        assert np.all(np.max(x, axis=0) < self.structure), f'Values out of range'
        # check that values are positive
        assert np.all(x >= 0), f'Negative values'

        x1 = np.zeros((x.shape[0], self.length), dtype=np.float64)
        start = 0
        for i in range(self.n):
            x1[np.arange(x.shape[0]), x[:, i] + start] = 1
            start += self.structure[i]
        return x1

    def onehot_to_values(self, x: np.array):
        """Return the original values from the one-hot encoding"""
        assert len(x.shape) == 2, f'{len(x.shape)} != 2'
        assert x.shape[1] == self.length, f'{x.shape[1]} != {self.length}'
        x1 = np.zeros((x.shape[0], self.n), dtype=np.int64)
        start = 0
        for i in range(self.n):
            x1[:, i] = np.argmax(x[:, start:start + self.structure[i]], axis=1)
            start += self.structure[i]
        return x1

    def prob_to_onehot(self, p: np.array):
        """Convert the probabilities to one-hot encoding"""
        assert len(p.shape) == 2, f'{len(p.shape)} != 2'
        assert p.shape[1] == self.length, f'{p.shape[1]} != {self.length}'
        x = np.zeros((p.shape[0], self.n), dtype=np.int64)
        start = 0
        for i in range(self.n):
            x[:, i] = np.argmax(p[:, start:start + self.structure[i]], axis=1)
            start += self.structure[i]
        return self.to_onehot(x)
    
    def add_noise(self, p: np.array, k=1.):
        """Add noise to the probabilities"""
        assert len(p.shape) == 2, f'{len(p.shape)} != 2'
        assert p.shape[1] == self.length, f'{p.shape[1]} != {self.length}'
        return self.normalize(p + np.random.random(p.shape) * k)

    def _logits_to_normalized_probs(self, logits):
        """Convert logits to normalized probabilities"""
        assert isinstance(logits, np.ndarray), 'logits must be a numpy array'
        p = 1 / (1 + np.exp(-logits))
        return self.normalize(p)
    
    def prob_to_values(self, p):
        """Convert probabilities to values"""
        if isinstance(p, torch.Tensor):
            p = p.numpy()
        return self.onehot_to_values(self.prob_to_onehot(self.normalize(p)))

    def logits_to_proba(self, logits):
        """Convert logits to probabilities"""
        if isinstance(logits, torch.Tensor):
            logits = logits.numpy()
        return self._logits_to_normalized_probs(logits)

    def logits_to_values(self, logits):
        """Convert logits to values"""
        if isinstance(logits, torch.Tensor):
            logits = logits.numpy()
        p = self._logits_to_normalized_probs(logits)
        return self.onehot_to_values(self.prob_to_onehot(p))
    
    def values_to_logits(self, values, epsilon=1e-6):
        """Convert values to logits"""
        p = self.to_onehot(values)
        return np.log((p + epsilon) / (1 - p + epsilon))


class bcolors:
    """ Author: Omar
    This class helps to print colored text in the terminal.
    To uce this class, call cprint(text, color)
    """
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def cprint(text, color, end='\n'):
    """ Author: Omar
    Colorful print function. To see the colors, go to the class bcolors.

    Usage:
    cprint('You may fluff her tai', bcolors.OKGREEN)
    cprint('Warning: no sadge allowed', bcolors.WARNING)
    cprint('Failed to be sadge', bcolors.FAIL)
    """
    print(color + text + bcolors.ENDC, end=end)


def plot_agreement_disagreement_transformation(array1, array2, filename, save_locally=False, path="../plots/"):
    """
    Plot the agreement and disagreement between two boolean arrays and show transformations.
    
    Parameters:
    - array1 (np.ndarray): First boolean array.
    - array2 (np.ndarray): Second boolean array.
    """
    # Ensure the inputs are boolean arrays
    assert array1.dtype == bool and array2.dtype == bool, "Inputs must be boolean arrays"
    assert len(array1) == len(array2), "Arrays must be of the same length"

    # Identify agreement and disagreement
    agree = array1 == array2
    disagree = ~agree

    # Identify proper transformations (True in array1 to False in array2)
    proper_transformed = (array1 & ~array2)
    # Identify wrong transformations (False in array1 to True in array2)
    wrong_transformed = (~array1 & array2)

    # Create the plot with higher DPI
    plt.figure(figsize=(30, 7), dpi=300)

    y_ticks = np.array([1, 2, 3, 4, 5])
    pm = 0.1

    # Plot array1 representation
    plt.vlines(np.where(array1)[0], y_ticks[4] - pm, y_ticks[4] + pm, color='c', label='Orig.: T')
    plt.vlines(np.where(~array1)[0], y_ticks[4] - pm, y_ticks[4] + pm, color='y', label='Orig.: F')

    # Plot array2 representation
    plt.vlines(np.where(array2)[0], y_ticks[3] - pm, y_ticks[3] + pm, color='b', label='Trans.: T')
    plt.vlines(np.where(~array2)[0], y_ticks[3] - pm, y_ticks[3] + pm, color='m', label='Trans.: F')

    # Plot agreement
    plt.vlines(np.where(agree)[0], y_ticks[2] - pm, y_ticks[2] + pm, color='g', label='Agree')

    # Plot disagreement
    plt.vlines(np.where(disagree)[0], y_ticks[2] - pm, y_ticks[2] + pm, color='r', label='Disagree')

    # Plot proper transformations
    plt.vlines(np.where(wrong_transformed)[0], y_ticks[1] - pm, y_ticks[1] + pm, color='b', label='x Trans. (F to T)')

    # Plot wrong transformations
    plt.vlines(np.where(proper_transformed)[0], y_ticks[0] - pm, y_ticks[0] + pm, color='m', label='✓ Trans. (T to T)')

    # Add grid and labels
    plt.yticks(y_ticks, ['', '', '', '', ''])
    plt.xlabel('Index')
    plt.title('Agreement, Disagreement, and Transformation between Two Boolean Arrays')
    plt.legend(bbox_to_anchor=(1.005, 1), loc='upper left')
    plt.grid(True, which='both', linestyle='--', linewidth=0.5)

    # Remove x and y ticks
    plt.xticks([])

    # Save the plot locally
    if save_locally:
        plt.savefig(path + filename + '.png')

    # Save the plot to wandb
    wandb.log({filename: wandb.Image(plt)})


def plot_categories(label_values, n_values, filename, save_locally=False, save_wandb=False, path="../plots/"):
    if isinstance(label_values, torch.Tensor):
        label_values = label_values.numpy()

    data = pd.DataFrame(label_values, columns=[f'Category {i}' for i in range(len(n_values))])

    # Melt the dataframe to have a long format suitable for seaborn
    melted_data = data.melt(var_name='Category', value_name='Value')

    # Create a figure with subplots for each category
    fig, axs = plt.subplots(1, len(n_values), figsize=(30, 6), sharey=True)

    # Get the maximum number of unique values in all categories
    max_unique_values = max(data.nunique())

    # Iterate over each category
    for i, category in enumerate(data.columns):
        # Create the bar plot for the current category
        ax = sns.countplot(data=melted_data[melted_data['Category'] == category], x='Category', hue='Value', ax=axs[i])

        # Adjust the width of the bars according to the number of unique values in the category
        for patch in ax.patches:
            current_width = patch.get_width()
            diff = current_width - (current_width * data[category].nunique() / max_unique_values)
            patch.set_width(current_width - diff)

        # Adding labels and title
        axs[i].set_ylabel('Count') if i == 0 else axs[i].set_ylabel('')
        axs[i].legend(title='Values')
        axs[i].set_xlabel('')  # Remove the x-label
        axs[i].grid(True)

    # Display the plots
    plt.tight_layout()

    # Save the plot locally
    if save_locally:
        plt.savefig(path + filename + '.png')

    # Save the plot to wandb
    if save_wandb:
        wandb.log({filename: wandb.Image(plt)})


def element_wise_label_values_comparison(input, output, mask):
    """
    Compares the input array with the output array of the inpainting method
    using a mask to identify differences. It returns the number of rows with
    differences, the total number of values that should not have changed according
    to the mask, and the total number of values that were actually changed with respect
    to the mask.
    """
    # Check if the mask is compatible with the arrays
    if input.shape != output.shape or input.shape != mask.shape:
        raise ValueError("Array shapes and mask shape must match.")
    
    if isinstance(input, torch.Tensor):
        input = input.numpy().astype(int)
    if isinstance(output, torch.Tensor):
        output = output.numpy().astype(int)
    if isinstance(mask, torch.Tensor):
        mask = mask.numpy().astype(bool)

    num_rows_differ = 0
    total_wrongly_changed_values = 0
    known_values = 0

    for row_input, row_output, row_mask in zip(input, output, mask):
        # Apply the mask to the current row
        masked_input = row_input[~row_mask]
        masked_output = row_output[~row_mask]

        # Compare the masked arrays element-wise
        element_wise_comparison = masked_input != masked_output

        # Calculate the total number of values wrongly changed for this row
        row_wrongly_changed_values = np.sum(element_wise_comparison)

        # Update the total wrongly changed values
        total_wrongly_changed_values += row_wrongly_changed_values

        # Update the total number of values that should not have changed according to the mask
        row_known_values = np.sum(~row_mask)
        known_values += row_known_values

        # Determine if there are differences within the masked input for this row
        if np.any(element_wise_comparison):
            num_rows_differ += 1

    return num_rows_differ, known_values, total_wrongly_changed_values


class ClassificationModel:
    """
    Example classifier
    """

    def __init__(self):
        self.model = None
        
    def load_model_pickle(self, filename, path="../models/"):
        """Load model parameters from a file using pickle."""
        print(f'Loading a classifier model...')
        try:
            model = torch.load(path + filename + '.pkl')
            self.model = model
        except FileNotFoundError:
            print('Model not found')
            self.model = None

    def __call__(self, *args, **kwargs):
        return self.model(*args, **kwargs)

    def reset(self, input_size, hidden):
        print(f'Creating a new classifier model...')
        self.model = nn.Sequential(
            nn.Linear(input_size, hidden),
            nn.Softplus(),
            nn.Linear(hidden, 1),
            nn.Sigmoid()
        )
        # self.model = nn.Sequential(
        #     nn.Linear(input_size, hidden),
        #     nn.ReLU(),
        #     nn.Dropout(0.5),
        #     nn.Linear(hidden, 2*hidden),
        #     nn.ReLU(),
        #     nn.Dropout(0.5),
        #     nn.Linear(2*hidden, hidden),
        #     nn.ReLU(),
        #     nn.Dropout(0.5),
        #     nn.Linear(hidden, 1),
        #     nn.Sigmoid()
        # )

    def _training_loop(self, dataloader, n_epochs, learning_rate, weight_decay):
        # use the AdamW optimizer
        optimizer = optim.AdamW(self.model.parameters(), lr=learning_rate, weight_decay=weight_decay)
        # use the Binary Cross Entropy loss
        criterion = nn.BCELoss()
        
        pbar = tqdm(range(n_epochs))
        for epoch in pbar:
            self.model.train()
            running_loss = 0.0
            num_elements = 0
            
            for X_batch, y_batch in dataloader:
                optimizer.zero_grad()

                y_pred = self.model(X_batch)
                loss = criterion(y_pred, y_batch)

                loss.backward()
                optimizer.step()

                running_loss += loss.item()
                num_elements += X_batch.shape[0]
            epoch_loss = running_loss / num_elements
            pbar.set_description(f'Epoch: {epoch+1} | Loss: {epoch_loss:.5f}')
        
    def train(self, dataloader, n_epochs=200, learning_rate=0.1, weight_decay=1e-3,
              model_name="classifier_ddpm", path="../models/"):
        self._training_loop(dataloader, n_epochs, learning_rate, weight_decay)

        x = dataloader.dataset.dataset.tensors[0]
        y = dataloader.dataset.dataset.tensors[1]
        # test the model
        y_pred = self.model(x)

        # performance metrics
        y_class = (y_pred > 0.5).float()
        accuracy = np.array(y_class == y).astype(float).mean()
        dummy_acc = max(y.mean().item(), 1 - y.mean().item())
        acc = accuracy.item()
        usefulness = max([0, (acc - dummy_acc) / (1 - dummy_acc)])
        print(f'Dummy accuracy = {dummy_acc:.1%}')
        print(f'Accuracy = {acc:.1%}')
        print(f'Usefulness = {usefulness:.1%}')

        if not os.path.exists(path):
            os.makedirs(path)
            # save the model
        torch.save(self.model, path + model_name + '.pkl')
        

def compute_arrays_agreements(array1, array2):
    if isinstance(array1, torch.Tensor):
        array1 = array1.numpy().astype(int)
    if isinstance(array2, torch.Tensor):
        array2 = array2.numpy().astype(int)
    
    # Compute the number of columns they agree on per row
    agreements_per_row = np.sum(array1 == array2, axis=1)

    # Calculate the mean, median, and standard deviation of these values
    mean_agreements = np.mean(agreements_per_row)
    median_agreements = np.median(agreements_per_row)
    std_agreements = np.std(agreements_per_row)

    results = {
        "mean": mean_agreements,
        "median": median_agreements,
        "std": std_agreements
    }
    
    return results