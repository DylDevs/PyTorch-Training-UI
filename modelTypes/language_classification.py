from torch.utils.data import Dataset, DataLoader
import torch.optim.lr_scheduler as lr_scheduler
from torch.amp import GradScaler, autocast
import torch.optim as optim
import torch.nn as nn
import numpy as np
import traceback
import datetime
import random
import torch
import json
import time
import os

# Resources to create models for the Torch AI Training Hub
from modelTypes.modules import *

name = "Language Classification"
description = "Generates a number based on input text"
data_type = "text" # The type of data the model is trained on (text, image, audio, other)
hyperparameters = [
    Hyperparameter("data_path", "Data Path", fr"{os.path.dirname(os.path.dirname(__file__))}\data\language_classification", special_type="path", description="Path where the training data is stored"),
    Hyperparameter("model_path", "Model Path", fr"{os.path.dirname(os.path.dirname(__file__))}\models\language_classification", special_type="path", description="Path where the model will be saved"),
    Hyperparameter("device", "Device", "GPU" if torch.cuda.is_available() else "CPU", special_type="dropdown", options=["GPU", "CPU"], description="Device to train on"),
    Hyperparameter("epochs", "Epochs", 100, min_value=1, description="Number of epochs to train for"),
    Hyperparameter("batch_size", "Batch Size", 64, min_value=1, description="Number of samples per batch"),
    Hyperparameter("classes", "Classes", 2, min_value=2, description="Number of classes to predict"),
    Hyperparameter("learning_rate", "Learning Rate", 0.001, min_value=0.00001, max_value=1, incriment=0.0001, description="Learning rate for the optimizer"),
    Hyperparameter("max_learning_rate", "Max Learning Rate", 0.01, min_value=0.00001, max_value=1, incriment=0.0001, description="Maximum learning rate for the optimizer"),
    Hyperparameter("train_val_ratio", "Train to Val Ratio", 0.9, min_value=0, max_value=0.99, incriment=0.01, description="Ratio of training data to validation data"),
    Hyperparameter("num_workers", "Num Workers", 0, min_value=0, description="Number of workers to use for data loading"),
    Hyperparameter("dropout", "Dropout", 0.2, min_value=0, max_value=1, incriment=0.01, description="Probability that a neuron will skip a forward pass"),
    Hyperparameter("patience", "Patience", 25, min_value=0, description="Number of epochs to wait before early stopping"),
    Hyperparameter("shuffle_train", "Shuffle Train", True, description="Whether to shuffle the training data"),
    Hyperparameter("shuffle_val", "Shuffle Val", True, description="Whether to shuffle the validation data"),
    Hyperparameter("shuffle_each_epoch", "Shuffle Each Epoch", True, description="Whether to shuffle the training data and validation data (if shuffle_train and shuffle_val are True) each epoch"),
    Hyperparameter("pin_memory", "Pin Memory", False),
    Hyperparameter("drop_last", "Drop Last", False),
    Hyperparameter("embedding_dim", "Embedding Dim", 128, min_value=1),
    Hyperparameter("hidden_dim", "Hidden Dim", 512, min_value=1)
]

class NeuralNetwork(nn.Module):
    def __init__(self, vocab_size, embedding_dim, hidden_dim, output_dim, dropout_rate, max_length):
        super(NeuralNetwork, self).__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim, padding_idx=0)
        self.fc1 = nn.Linear(2 * max_length * embedding_dim, hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, output_dim)
        self.dropout = nn.Dropout(dropout_rate)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, text):
        embedded = self.embedding(text).view(text.size(0), -1)
        x = self.dropout(torch.relu(self.fc1(embedded)))
        return self.softmax(self.fc2(x))

class Model(ModelTemplate):
    def __init__(self, hyperparameters : list[Hyperparameter]) -> None:
        self.hyperparameters : list[Hyperparameter] = hyperparameters
        self.required_training_data : RequiredTrainingData = None
        self.additional_training_data : AdditionalTrainingData = None
        self.model_data : ModelData = None
        self.device : torch.device = torch.device("cuda" if self.GetHyp("device") == "GPU" else "cpu")

        self.hyp_fetcher : HyperparameterFetcher = HyperparameterFetcher(hyperparameters)

        self.error : str = None
        self.exception : str = None

    def GetHyp(self, name : str) -> str | int | float | bool:
        """Get a hyperparameter from the hyperparameter fetcher."""
        return self.hyp_fetcher.GetHyp(name)

    def load_data(data_folder) -> list[list[str, str, int]]:
        '''
        Load language classification data from the specified folder.

        Args:
            data_folder (str): The folder containing the data
        
        Returns:
            list[list[str, str, int]]: A list of lists containing the message, channel, and output
        '''
        data = []
        for filename in os.listdir(data_folder):
            if filename.endswith(".txt"):
                with open(os.path.join(data_folder, filename), 'r') as file:
                    lines = file.readlines()
                    message = lines[0].strip()
                    channel = lines[1].strip()
                    output = int(lines[2].strip())
                    data.append((message, channel, output))
        return data

    class CustomDataset(Dataset):
        def __init__(self, data, max_length):
            self.data = data
            self.max_length = max_length
            self.vocab = self.build_vocab()

        def build_vocab(self):
            vocab = set()
            for message, channel, _ in self.data:
                vocab.update(message)
                vocab.update(channel)
            return {char: idx + 1 for idx, char in enumerate(vocab)}

        def encode(self, text):
            encoded = [self.vocab.get(char, 0) for char in text]
            return encoded + [0] * (self.max_length - len(encoded))

        def __len__(self):
            return len(self.data)

        def __getitem__(self, idx):
            message, channel, label = self.data[idx]
            message_encoded = self.encode(message)[:self.max_length]
            channel_encoded = self.encode(channel)[:self.max_length]
            return torch.tensor(message_encoded + channel_encoded), torch.tensor(label)
        
    def GetMaxLength(data):
        """
        Find the maximum length of a message in the data.

        Args:
            data (list): The data to find the maximum length from.

        Returns:
            int: The maximum length of a message in the data.
        """
        max_length = 0
        for message, _, _ in data:
            if len(message) > max_length:
                max_length = len(message)
        return max_length
        
    def SplitDataset(self, data, train_val_ratio, max_length) -> tuple[CustomDataset, CustomDataset, int, int]:
        """
        Split the data into train and validation sets.

        Args:
            data (list): The data to split.
            train_val_ratio (float): The ratio of train data to validation data.
            max_length (int): The maximum length of a message in the data.

        Returns:
            tuple: A tuple containing the train and validation datasets along with the number of train and validation data points.
        """
        datapoints = len(data)
        train_amount = int(train_val_ratio * datapoints)
        val_amount = datapoints - train_amount

        train_indices = random.sample(range(datapoints), train_amount)
        val_indices = [i for i in range(datapoints) if i not in train_indices]

        train_data = []
        val_data = []

        for i in train_indices:
            train_data.append(data[i])
        for i in val_indices:
            val_data.append(data[i])

        train_dataset = self.CustomDataset(train_data, max_length=max_length)
        val_dataset = self.CustomDataset(val_data, max_length=max_length)

        return train_dataset, val_dataset, train_amount, val_amount
    
    def CreateTrainDataLoader(self):
        self.train_dataloader = DataLoader(self.train_dataset, batch_size=self.GetHyp("batch_size"), shuffle=self.GetHyp("shuffle_train"), num_workers=self.GetHyp("num_workers"), pin_memory=self.GetHyp("pin_memory"), drop_last=self.GetHyp("drop_last"))
    
    def CreateValDataLoader(self):
        self.val_dataloader = DataLoader(self.val_dataset, batch_size=self.GetHyp("batch_size"), shuffle=self.GetHyp("shuffle_val"), num_workers=self.GetHyp("num_workers"), pin_memory=self.GetHyp("pin_memory"), drop_last=self.GetHyp("drop_last"))

    def GetModelSize(model : nn.Module):
        """Get the estimated size of a model in MB."""
        total_params = 0
        for param in model.parameters():
            total_params += np.prod(param.size())
        trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        non_trainable_params = total_params - trainable_params
        bytes_per_param = next(model.parameters()).element_size()
        model_size_mb = (total_params * bytes_per_param) / (1024 ** 2)
        return total_params, trainable_params, non_trainable_params, model_size_mb

    def Initialize(self):
        """This function sets up the model and data for training. (Called by the training controller)"""
        try:
            data = self.load_data(self.GetHyp("data_folder"))
            self.data_length = len(data)
            max_length = self.GetMaxLength(data)

            self.model = NeuralNetwork(vocab_size, self.GetHyp("embedding_dim"), self.GetHyp("hidden_dim"), self.GetHyp("classes"), self.GetHyp("dropout"), self.GetHyp("max_length"))
            
            self.train_dataset, self.val_dataset, self.train_size, self.val_size = self.SplitDataset(data, self.GetHyp("train_val_ratio"), max_length)
            vocab_size = len(self.train_dataset.vocab) + 1

            self.CreateTrainDataLoader()
            self.CreateValDataLoader()

            self.scaler = GradScaler()
            self.criterion = nn.CrossEntropyLoss()
            self.optimizer = optim.Adam(self.model.parameters(), lr=self.GetHyp("learning_rate"))
            self.scheduler = lr_scheduler.OneCycleLR(self.optimizer, max_lr=self.GetHyp("max_learning_rate"), steps_per_epoch=len(self.train_dataloader), epochs=self.GetHyp("epochs"))
        
            total_params, trainable_params, non_trainable_params, model_size_mb = self.GetModelSize(self.model)

            self.additional_training_data = [
                AdditionalTrainingData("train_size", "Train Size", self.train_size),
                AdditionalTrainingData("val_size", "Val Size", self.val_size),
                AdditionalTrainingData("max_length", "Max Length", max_length),
                AdditionalTrainingData("vocab_size", "Vocab Size", vocab_size)
            ]

            self.model_data = [
                ModelData("total_params", "Total Parameters", total_params),
                ModelData("trainable_params", "Trainable Parameters", trainable_params),
                ModelData("non_trainable_params", "Non-trainable Parameters", non_trainable_params),
                ModelData("model_size_mb", "Model Size (MB)", model_size_mb),
                ModelData("scaler", "Scaler", type(self.scaler).__name__),
                ModelData("criterion", "Criterion", type(self.criterion).__name__),
                ModelData("optimizer", "Optimizer", type(self.optimizer).__name__),
                ModelData("scheduler", "Scheduler", type(self.scheduler).__name__),
            ]

        except Exception as e:
            self.error = str(e)
            self.exception = traceback.format_exc()

    def Train(self):
        """This function trains the model. (Called by the training controller every epoch)"""
        try:
            if self.GetHyp("shuffle_each_epoch"):
                self.CreateTrainDataLoader() if self.GetHyp("shuffle_train") else None
                self.CreateValDataLoader() if self.GetHyp("shuffle_val") else None

            # Training phase
            self.model.train()
            running_training_loss = 0.0
            for i, data in enumerate(self.train_dataloader, 0):
                inputs, labels = data[0].to(self.device, non_blocking=True), data[1].to(self.device, non_blocking=True)
                self.optimizer.zero_grad()
                with autocast(device_type=str(self.device)):
                    outputs = self.model(inputs)
                    loss = self.criterion(outputs, labels)
                self.scaler.scale(loss).backward()
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.scheduler.step()
                running_training_loss += loss.item()
            running_training_loss /= len(self.train_dataloader)
            training_loss = running_training_loss

            # Validation phase
            self.model.eval()
            running_validation_loss = 0.0
            with torch.no_grad(), autocast(device_type=str(self.device)):
                for i, data in enumerate(self.val_dataloader, 0):
                    inputs, labels = data[0].to(self.device, non_blocking=True), data[1].to(self.device, non_blocking=True)
                    outputs = self.model(inputs)
                    loss = self.criterion(outputs, labels)
                    running_validation_loss += loss.item()
            running_validation_loss /= len(self.val_dataloader)
            validation_loss = running_validation_loss

            torch.cuda.empty_cache()

            self.required_training_data = RequiredTrainingData(training_loss, validation_loss)
        except Exception as e:
            self.error = str(e)
            self.exception = traceback.format_exc()

    def Save(self, model : NeuralNetwork, training_data : RequiredTrainingData):
        """This function saves the model. (Called by the training controller)"""
        try:
            torch.cuda.empty_cache()

            model.eval()
            total_train = 0
            correct_train = 0
            with torch.no_grad():
                for data in self.train_dataloader:
                    inputs, labels = data[0].to(self.device, non_blocking=True), data[1].to(self.device, non_blocking=True)
                    outputs = model(inputs)
                    _, predicted = torch.max(outputs.data, 1)
                    total_train += labels.size(0)
                    correct_train += (predicted == labels).sum().item()
            training_dataset_accuracy = str(round(100 * (correct_train / total_train), 2)) + "%"

            torch.cuda.empty_cache()

            total_val = 0
            correct_val = 0
            with torch.no_grad():
                for data in self.val_dataloader:
                    inputs, labels = data[0].to(self.device, non_blocking=True), data[1].to(self.device, non_blocking=True)
                    outputs = model(inputs)
                    _, predicted = torch.max(outputs.data, 1)
                    total_val += labels.size(0)
                    correct_val += (predicted == labels).sum().item()
            validation_dataset_accuracy = str(round(100 * (correct_val / total_val), 2)) + "%"

            torch.cuda.empty_cache()

            # Metadata as a dictionary for JSON format
            last_model_metadata = {
                "training_os": os.name,
                "architecture": str(model).replace('\n', ''),
                "torch_version": torch.__version__,
                "numpy_version": np.__version__,
                "optimizer": str(self.optimizer).replace('\n', ''),
                "loss_function": str(self.criterion).replace('\n', ''),
                "training_size": self.train_size,
                "validation_size": self.val_size,
                "training_loss": training_data.training_loss,
                "validation_loss": training_data.validation_loss,
                "dataset_length": self.data_length,
                "training_dataset_accuracy": training_dataset_accuracy,
                "validation_dataset_accuracy": validation_dataset_accuracy
            }

            metadata = {"metadata": json.dumps(last_model_metadata).encode("utf-8")}
            model_name = f"{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.pt"

            if not os.path.exists(self.GetHyp("model_path")):
                os.makedirs(self.GetHyp("model_path"))
            torch.jit.save(torch.jit.script(model), os.path.join(self.GetHyp("model_path"), model_name), _extra_files=metadata)
            print(f"Saved {model_name} successfully.")
        except Exception as e:
            self.error = str(e)
            self.exception = traceback.format_exc()