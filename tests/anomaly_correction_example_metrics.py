import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import torch
import pandas as pd
import numpy as np
from tqdm import tqdm
import io
import contextlib
from functools import wraps
from src.utils import cprint, bcolors, plot_loss
from src.denoising_diffusion_pm import DDPMAnomalyCorrection as Diffusion
from src.anomaly_correction import AnomalyCorrection

# set default type to avoid problems with gradient
DEFAULT_TYPE = torch.float64
torch.set_default_dtype(DEFAULT_TYPE)


class ClassificationModel:
    """
    Example classifier
    """

    def __init__(self):
        self.model = None

    def load_from_file(self, model_path):
        """
        Set the model if the pkl file is found.
        If a file is not found, then the model remains None
        """
        if os.path.exists(model_path):
            try:
                cprint(f'Loading model from {model_path}', bcolors.WARNING)
                self.model = torch.load(model_path)
                cprint('Model loaded', bcolors.OKGREEN)
            except FileNotFoundError:
                cprint('Model not found', bcolors.FAIL)

    def __call__(self, *args, **kwargs):
        return self.model(*args, **kwargs)

    def reset(self, input_size, hidden):
        """ Create the model """
        print(f'Input size: {input_size}')
        cprint('Creating model', bcolors.WARNING)
        self.model = torch.nn.Sequential(
            torch.nn.Linear(input_size, hidden),
            torch.nn.Softplus(),
            torch.nn.Linear(hidden, 1),
            torch.nn.Sigmoid()
        )

    def _training_loop(self, num_samples, optimizer, n_epochs, batch_size, x, y, loss_fn):

        for epoch in range(n_epochs):
            total_loss = 0.0
            for i in range(0, num_samples, batch_size):
                batch_x = x[i:i + batch_size]
                batch_y = y[i:i + batch_size]

                y_pred = self.model(batch_x)
                loss = loss_fn(y_pred, batch_y)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                total_loss += loss.item()

            avg_loss = total_loss / (num_samples / batch_size)
            print(f'\rEpoch {epoch + 1}, Loss {avg_loss:.6f}', end=' ')
        print()

    def train(self, x, y, model_path, loss_fn, n_epochs=200, lr=0.1, weight_decay=1e-4,
              momentum=0.9, nesterov=True, batch_size=100):
        # optimizer
        optimizer = torch.optim.SGD(self.model.parameters(), lr=lr,
                                    weight_decay=weight_decay, momentum=momentum,
                                    nesterov=nesterov)

        # Training loop
        num_samples = x.shape[0]
        self._training_loop(num_samples, optimizer, n_epochs, batch_size, x, y, loss_fn)

        # test the model
        y_pred = self.model(x)
        loss = loss_fn(y_pred, y)
        print(f'Final Loss {loss.item():.6f}')

        # performance metrics
        y_class = (y_pred > 0.5).float()
        accuracy = np.array(y_class == y).astype(float).mean()
        dummy_acc = max(y.mean().item(), 1 - y.mean().item())
        acc = accuracy.item()
        usefulness = max([0, (acc - dummy_acc) / (1 - dummy_acc)])
        if usefulness > 0.75:
            color = bcolors.OKGREEN
        elif usefulness > 0.25:
            color = bcolors.WARNING
        else:
            color = bcolors.FAIL
        print(f'Dummy accuracy = {dummy_acc:.1%}')
        print(f'Accuracy on test data = {acc:.1%}')
        cprint(f'usefulness = {usefulness:.1%}', color)
        rmse = torch.sqrt(torch.mean((y_pred - y) ** 2))
        print(f'RMSE on test data {rmse.item():.3f}')

        # save the model
        torch.save(self.model, model_path)
        cprint('Model saved', bcolors.OKGREEN)

def suppress_print(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Capture stdout
        with io.StringIO() as buf, contextlib.redirect_stdout(buf):
            # Capture stderr (used by tqdm)
            with io.StringIO() as buf_err, contextlib.redirect_stderr(buf_err):
                result = func(*args, **kwargs)
                output = buf.getvalue()
                error_output = buf_err.getvalue()
        return result #, output, error_output
    return wrapper


def main(data_path='../datasets/sum_limit_problem.csv',
         model_path='../models/anomaly_correction_model.pkl',
         hidden=10, loss_fn=torch.nn.MSELoss(), n_epochs=250):
    np.random.seed(42)

    # ================================================================================
    # get data
    df_x = pd.read_csv(data_path)
    df_x = df_x.sample(frac=1).reset_index(drop=True)
    df_y = df_x.copy()['anomaly']
    del df_x['anomaly']

    # ================================================================================
    # anomaly_correction
    anomaly_correction = AnomalyCorrection(df_x, df_y, noise=1.)
    # print('\nNoisy probabilities:')
    # print(np.round(anomaly_correction.p_data_noisy, 2))
    # print('\nValue maps:')
    # for key in anomaly_correction.get_value_maps():
    #     print(f'{key}: {anomaly_correction.get_value_maps()[key]}')

    # ================================================================================
    # The classification model
    data_x, data_y = anomaly_correction.get_classification_dataset()

    classification_model = ClassificationModel()
    classification_model.load_from_file(model_path)

    if classification_model.model is None:
        classification_model.reset(input_size=data_x.shape[1], hidden=hidden)
        classification_model.train(data_x, data_y,
                                   model_path, loss_fn, n_epochs=n_epochs)

    anomaly_correction.set_classification_model(classification_model)

    # ================================================================================
    # The diffusion model
    data_diff_x = anomaly_correction.get_diffusion_dataset()
    dataset_shape = [1, sum(anomaly_correction.structure)]
    # print(f'\ndataset_shape = {dataset_shape}')

    # # in index space
    # print(f'\ndata_diff_x {data_diff_x.shape}')
    # print(data_diff_x)

    # This class takes as input the anomaly and the masks, and returns the modified anomalies
    diffusion = Diffusion(dataset_shape=dataset_shape,
                          noise_time_steps=128)

    ddpm_model_name = 'ddpm_model'

    diffusion.load_model_pickle(ddpm_model_name)  # !name, NOT PATH

    if diffusion.model is None:
        diffusion.set_model(time_dim_emb=64,
                            concat_x_and_t=True,
                            feed_forward_kernel=True,
                            hidden_units=[2 * dataset_shape[1],
                                          3 * dataset_shape[1],
                                          2 * dataset_shape[1]
                                          ],
                            unet=False)
        # DDPM training
        train_losses = diffusion.train(data_diff_x,
                                       batch_size=16,
                                       learning_rate=1e-3,
                                       epochs=100,
                                       beta_ema=0.999,
                                       plot_data=True,
                                       proba=anomaly_correction.proba,
                                       original_data_name='ddpm_original_data')
        loss_name = 'ddpm_loss'

        plot_loss(train_losses, loss_name, save_locally=True)
        diffusion.save_model_pickle(filename=ddpm_model_name,
                                    ema_model=True)

    anomaly_correction.set_diffusion(diffusion)

    diffusion.sample(num_samples=1000,
                     classifier=classification_model,
                     plot_data=True,
                     proba=anomaly_correction.proba,
                     sampled_data_name='ddpm_sampled_data')

    # # ================================================================================
    # # pick some anomalies
    # anomaly = df_x[df_y == 1].sample(1)
    # print('\nAnomaly:')
    # print(anomaly)
    # print(type(anomaly))

    # # ================================================================================
    # # run the anomaly-correction algorithm
    # corrected_anomaly = suppress_print(anomaly_correction.correct_anomaly)(anomaly, n=10)
    # print('\nCorrected anomaly:')
    # print(corrected_anomaly)
    
    # stop the code execution here
    # return
    
    anomalies = df_x[df_y == 1]
    
    corrected_anomalies = []
    pbar = tqdm(range(anomalies.shape[0]))
    for i in pbar:
        anomaly = anomalies.iloc[i, :].to_frame().transpose()
        # corrected_anomaly = suppress_print(anomaly_correction.correct_anomaly)(anomaly, n=10)
        corrected_anomaly = anomaly_correction._anomaly_to_proba(anomaly, n=10)
        corrected_anomalies.append(corrected_anomaly)
        if i == 2:
            pbar.close()  # Close the display of the progress bar
            break

    print('\nCoorrected anomalies Inverse Gradient:')
    
    # print the first elements in each entry of the list
    print(len(corrected_anomalies))
    print(corrected_anomalies[0].iloc[0, :].to_frame().transpose())
    print(corrected_anomalies[1].iloc[0, :].to_frame().transpose().values)
    print(corrected_anomalies[2].iloc[0, :].to_frame().transpose().values)
    
    # Extract the rows
    corrected_anomalies_per_mask = [[df.iloc[i].tolist() for df in corrected_anomalies] for i in range(len(corrected_anomalies[0]))] 

    # Convert the list of lists to a list of dataframes
    corrected_anomalies_per_mask = [pd.DataFrame(data, columns=corrected_anomalies[0].columns) for data in corrected_anomalies_per_mask]
    
    print('\nCoorrected anomalies Diffusion Inpainting:')
    print(corrected_anomalies_per_mask[0])
    
    mean, std = anomaly_correction.assessment(corrected_anomalies_per_mask)
    
    # mean and std of the corrected anomalies
    print(f'Percentage anomalies not corrected: {mean:.1%} ± {std:.1%}')
    

if __name__ == "__main__":
    main()
