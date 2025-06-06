from __future__ import annotations

import copy
import os
from inspect import isclass
from pathlib import PurePosixPath
from typing import Any

import fsspec
from darts import models
from darts.models.forecasting.torch_forecasting_model import TorchForecastingModel
from kedro.io.core import (
    AbstractVersionedDataset,
    DatasetError,
    Version,
    get_filepath_str,
    get_protocol_and_path,
)


class DartsTorchModelDataset(
    AbstractVersionedDataset[TorchForecastingModel, TorchForecastingModel]
):
    """DartsTorchModelDataset loads and saves Darts TorchForecastingModel instances.
    The underlying functionality is supported by, and passes arguments through to,
    the Darts library's model load and save methods.

    Example usage for the
    YAML API <https://kedro.readthedocs.io/en/stable/data/\
    data_catalog_yaml.html>_:

        .. code-block:: yaml

            darts_model:
              type: path.to.DartsTorchModelDataset
              filepath: data/06_models/darts_model.pt
              model_class: RNNModel
              load_args:
                load_method: load
              save_args:
                save_model: true
              versioned: true

    Example usage for the
    Python API <https://kedro.readthedocs.io/en/stable/data/\
    data_catalog_api.html>_:

    .. code-block:: python

        from path.to.your.module import DartsTorchModelDataset
        from darts.models import RNNModel
        from kedro.io.core import Version

        # Initialize the dataset
        dataset = DartsTorchModelDataset(
            filepath="data/06_models/darts_model.pt",
            model_class=RNNModel
        )

        # Assuming model is an instance of RNNModel
        model = RNNModel(input_chunk_length=12, output_chunk_length=6)

        # Save the model
        dataset.save(model)

        # Load the model
        loaded_model = dataset.load()

    """
    DEFAULT_LOAD_ARGS: dict[str, Any] = {}
    DEFAULT_SAVE_ARGS: dict[str, Any] = {}

    def __init__(  # noqa: PLR0913
            self,
            *,
            filepath: str,
            model_class: str | type[TorchForecastingModel],
            load_args: dict[str, Any] | None = None,
            save_args: dict[str, Any] | None = None,
            version: Version | None = None,
            credentials: dict[str, Any] | None = None,
            fs_args: dict[str, Any] | None = None,
            metadata: dict[str, Any] | None = None,
    ) -> None:
        """Creates a new instance of DartsTorchModelDataset.

        Args:
            filepath: Filepath in POSIX format to a model file or directory prefixed with a
                protocol like s3://. If prefix is not provided, the file protocol (local filesystem)
                will be used. The prefix should be any protocol supported by fsspec.
                Note: http(s) doesn't support versioning.
            model_class: The class of the model to load/save. Can be a string (name of the class in darts.models)
                or the class itself.
            load_args: Darts options for loading models.
                Available arguments depend on the load_method specified.
                All defaults are preserved.
            save_args: Darts options for saving models.
                Available arguments depend on the save method.
                All defaults are preserved.
            version: If specified, should be an instance of kedro.io.core.Version.
                If its load attribute is None, the latest version will be loaded.
                If its save attribute is None, save version will be autogenerated.
            credentials: Credentials required to access the underlying filesystem.
            fs_args: Extra arguments to pass into the underlying filesystem class constructor
                (e.g., {"project": "my-project"} for GCSFileSystem).
            metadata: Any arbitrary metadata.
                This is ignored by Kedro, but may be consumed by users or external plugins.
        """
        _fs_args = copy.deepcopy(fs_args) or {}
        _credentials = copy.deepcopy(credentials) or {}
        protocol, path = get_protocol_and_path(filepath, version)
        if protocol == "file":
            _fs_args.setdefault("auto_mkdir", True)

        self._protocol = protocol
        self._fs = fsspec.filesystem(self._protocol, **_credentials, **_fs_args)

        self.metadata = metadata

        # Handle model_class being a string or a class object
        if isinstance(model_class, str):
            try:
                self.model_class = getattr(models, model_class)
            except AttributeError as e:
                raise ValueError(
                    f"Model class '{model_class}' not found in darts.models."
                ) from e
        elif isclass(model_class) and issubclass(model_class, TorchForecastingModel):
            self.model_class = model_class
        else:
            raise ValueError(
                "model_class must be a string or a TorchForecastingModel subclass"
            )

        super().__init__(
            filepath=PurePosixPath(path),
            version=version,
            exists_function=self._fs.exists,
            glob_function=self._fs.glob,
        )

        self._load_args = {**self.DEFAULT_LOAD_ARGS, **(load_args or {})}
        self._save_args = {**self.DEFAULT_SAVE_ARGS, **(save_args or {})}

    def load(self) -> TorchForecastingModel:
        """
        Loads a TorchForecastingModel using the specified load method.

        Returns:
            An instance of TorchForecastingModel.
        """
        load_args = self._load_args.copy()
        load_method = load_args.pop("load_method", "load")

        if load_method == "load":
            load_path = get_filepath_str(self._get_load_path(), self._protocol)
            model = self.model_class.load(load_path, **load_args)
        elif load_method == "load_from_checkpoint":
            model_name = load_args.pop("model_name", None)
            if model_name is None:
                raise ValueError(
                    "model_name must be provided for 'load_from_checkpoint' method"
                )
            model = self.model_class.load_from_checkpoint(
                model_name=model_name, **load_args
            )
        elif load_method == "load_weights":
            load_path = get_filepath_str(self._get_load_path(), self._protocol)
            model = self.model_class.load_weights(load_path, **load_args)
        elif load_method == "load_weights_from_checkpoint":
            model_name = load_args.pop("model_name", None)
            if model_name is None:
                raise ValueError(
                    "model_name must be provided for 'load_weights_from_checkpoint' method"
                )
            model = self.model_class.load_weights_from_checkpoint(
                model_name=model_name, **load_args
            )
        else:
            raise ValueError(f"Unknown load method: {load_method}")
        return model

    def save(self, data: TorchForecastingModel) -> None:
        """
        Saves a TorchForecastingModel using the specified save method.

        Args:
            data: The TorchForecastingModel instance to save.
        """
        save_args = self._save_args.copy()
        save_model = save_args.pop("save_model", True)

        # Skipping model save as 'save_model' is set to False.
        if save_model:
            raw_path = self._get_save_path()
            os.makedirs(os.path.dirname(str(raw_path)), exist_ok=True)
            save_path = get_filepath_str(raw_path, protocol=self._protocol)
            data.save(save_path, **save_args)

    def _exists(self) -> bool:
        try:
            load_path = get_filepath_str(self._get_load_path(), self._protocol)
            return self._fs.exists(load_path)
        except DatasetError:
            return False

    def _describe(self) -> dict[str, Any]:
        return {
            "filepath": self._filepath,
            "protocol": self._protocol,
            "model_class": self.model_class.__name__,
            "load_args": self._load_args,
            "save_args": self._save_args,
            "version": self._version,
            "metadata": self.metadata,
        }

    def _release(self) -> None:
        super()._release()
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        filepath = get_filepath_str(self._filepath, self._protocol)
        self._fs.invalidate_cache(filepath)
