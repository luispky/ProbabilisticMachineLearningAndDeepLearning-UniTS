# Probabilistic Machine Learning + Deep Learning Project at the University of Trieste, 2023-2024

This project implements a process to perform anomaly correction of categorical data using two methods. One method uses the gradients of a classifier with respect to the inputs to transform a data input into a non-anomalous point. The other method leverages the distribution of the data learned by a diffusion model to correct data into a "healthy form". We also combined the two approaches into a pipeline to enhance the process. The first method produces a mask of the categories that could be modified using the distribution of the data learned by the diffusion model and then produces multiple versions of possible corrected instances. 

<div style="text-align: center;">
    <img src="https://github.com/user-attachments/assets/95af2f74-9eda-42f9-9f3c-b07c9f00af44" alt="CategoricalAnomalyCorrection" style="width: 50%;">
</div>
