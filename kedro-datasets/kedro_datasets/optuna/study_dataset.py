"""``StudyDataset`` loads/saves data from/to an optuna Study."""

from __future__ import annotations

import fnmatch
import logging
import os
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

import optuna
import pandas as pd
from kedro.io.core import (
    AbstractVersionedDataset,
    DatasetError,
    Version,
)
from sqlalchemy import URL

logger = logging.getLogger(__name__)


class StudyDataset(AbstractVersionedDataset[optuna.Study, optuna.Study]):
    """``StudyDataset`` loads/saves data from/to an optuna Study.

    Example usage for the
    `YAML API <https://docs.kedro.org/en/stable/data/data_catalog_yaml_examples.html>`_:

    .. code-block:: yaml

        review_prediction_study:
          type: optuna.StudyDataset
          backend: sqlite
          database: data/05_model_input/review_prediction_study.db
          load_args:
            sampler:
              class: TPESampler
              n_startup_trials: 10
              n_ei_candidates: 5
            pruner:
              class: NopPruner
          versioned: true

        price_prediction_study:
          type: optuna.StudyDataset
          backend: postgresql
          database: optuna_db
          credentials: dev_optuna_postgresql

    Example usage for the
    `Python API <https://docs.kedro.org/en/stable/data/\
    advanced_data_catalog_usage.html>`_:

    .. code-block:: pycon

        >>> from kedro_datasets.optuna import StudyDataset
        >>> import optuna
        >>>
        >>> study = optuna.create_study()
        >>> trial = optuna.trial.create_trial(
        >>>     params={"x": 2.0},
        >>>     distributions={"x": FloatDistribution(0, 10)},
        >>>     value=4.0,
        >>> study.add_trial(trial)
        >>>
        >>> dataset = StudyDataset(backend="sqlite", database="optuna.db")
        >>> dataset.save(study)
        >>> reloaded = dataset.load()
        >>> assert len(reloaded.trials) == 1
        >>> assert reloaded.trials[0].params["x"] == 2.0

    """

    DEFAULT_LOAD_ARGS: dict[str, Any] = {"sampler": None, "pruner": None}

    def __init__(  # noqa: PLR0913
        self,
        *,
        backend: str,
        database: str,
        study_name: str,
        load_args: dict[str, Any] | None = None,
        version: Version = None,
        credentials: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Creates a new instance of ``StudyDataset`` pointing to a concrete optuna
        Study on a specific relational database.

        Args:
            backend: Name of the database backend. This name should correspond to a module
                in ``SQLAlchemy``.
            database: Name of the database.
            study_name: Name of the optuna Study.
            load_args: Optuna options for loading studies. Accepts a `sampler` and a
                `pruner`. If either are provided, a `class` matching any Optuna `sampler`,
                respecitively `pruner` class name should be provided, optionally with
                their argyments. Here you can find all available samplers and pruners
                and their arguments:
                - https://optuna.readthedocs.io/en/stable/reference/samplers/index.html
                - https://optuna.readthedocs.io/en/stable/reference/pruners.html
                All defaults are preserved.
            version: If specified, should be an instance of
                ``kedro.io.core.Version``. If its ``load`` attribute is
                None, the latest version will be loaded. If its ``save``
                attribute is None, save version will be autogenerated.
            credentials: Credentials required to get access to the underlying RDB.
                They can include `username`, `password`, host`, and `port`.
            metadata: Any arbitrary metadata.
                This is ignored by Kedro, but may be consumed by users or external plugins.
        """
        self._backend = backend
        self._database = database

        credentials = dict(
            username=os.getenv("DB_USERNAME"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )

        _credentials = deepcopy(credentials) or {}
        credentials |= _credentials
        if _credentials:
            storage = URL.create(
                drivername=backend,
                database=database,
                **credentials,
            )

        self._storage = str(storage)
        self._study_name = study_name
        self.metadata = metadata

        filepath = None
        if backend == "sqlite":
            filepath = PurePosixPath(os.path.realpath(database))

        super().__init__(
            filepath=filepath,
            version=version,
            exists_function=self._study_name_exists,
            glob_function=self._study_name_glob,
        )

        # Handle default load and save and fs arguments
        self._load_args = {**self.DEFAULT_LOAD_ARGS, **(load_args or {})}

    def _get_versioned_path(self, version: str) -> PurePosixPath:
        study_name_posix = PurePosixPath(self._study_name)
        return study_name_posix / version / study_name_posix

    def resolve_load_version(self) -> str | None:
        """Compute the version the dataset should be loaded with."""
        if not self._version:
            return None
        if self._version.load:
            return self._version.load
        return self._fetch_latest_load_version()

    def _get_load_path(self) -> PurePosixPath:
        # Path is not affected by versioning
        return self._filepath

    def _get_load_study_name(self) -> str:
        if not self._version:
            # When versioning is disabled, load from original study name
            return self._study_name

        load_version = self.resolve_load_version()
        return str(self._get_versioned_path(load_version))

    def _get_save_path(self) -> PurePosixPath:
        # Path is not affected by versioning
        return self._filepath

    def _get_save_study_name(self) -> str:
        if not self._version:
            # When versioning is disabled, return original study name
            return self._study_name

        save_version = self.resolve_save_version()
        versioned_study_name = self._get_versioned_path(save_version)

        if self._exists_function(str(versioned_study_name)):
            raise DatasetError(
                f"Study name '{versioned_study_name}' for {self!s} must not exist if "
                f"versioning is enabled."
            )

        return str(versioned_study_name)

    def _describe(self) -> dict[str, Any]:
        return {
            "backend": self._backend,
            "database": self._database,
            "study_name": self._study_name,
            "load_args": self._load_args,
            "version": self._version,
        }

    # TODO: Support CmaEsSampler, QMCSampler, and GPSampler's independent_sampler
    # TODO: Support PartialFixedSampler's base_sampler
    def _get_sampler_from_config(self, sampler_config):
        if sampler_config is None:
            return None

        if "class" not in sampler_config:
            raise ValueError(
                "Optuna sampler `class` should be specified when trying to load study "
                f"named `{self._study_name}` with a sampler."
            )

        sampler_class = getattr(optuna.samplers, sampler_config.pop("class"))

        return sampler_class(**sampler_config)

    # TODO: Support PatientPruner's wrapped_pruner
    def _get_pruner_from_config(self, pruner_config):
        if pruner_config is None:
            return None

        if "class" not in pruner_config:
            raise ValueError(
                "Optuna pruner `class` should be specified when trying to load study "
                f"named `{self._study_name}` with a pruner."
            )

        pruner_class = getattr(optuna.pruners, pruner_config.pop("class"))

        return pruner_class(**pruner_config)

    def load(self) -> pd.DataFrame:
        load_args = deepcopy(self._load_args)
        sampler_config = load_args.pop("sampler")
        sampler = self._get_sampler_from_config(sampler_config)

        pruner_config = load_args.pop("pruner")
        pruner = self._get_pruner_from_config(pruner_config)

        study = optuna.load_study(
            storage=self._storage,
            study_name=self._get_load_study_name(),
            sampler=sampler,
            pruner=pruner,
        )

        return study

    def save(self, study: optuna.Study) -> None:
        save_study_name = self._get_save_study_name()
        if self._backend == "sqlite":
            os.makedirs(os.path.dirname(self._filepath), exist_ok=True)

            if not os.path.isfile(self._filepath):
                optuna.create_study(
                    storage=self._storage,
                )

        if self._study_name_exists(save_study_name):
            optuna.delete_study(
                storage=self._storage,
                study_name=save_study_name,
            )

        optuna.copy_study(
            from_study_name=study.study_name,
            from_storage=study._storage,
            to_storage=self._storage,
            to_study_name=save_study_name,
        )

    def _study_name_exists(self, path) -> bool:
        if self._backend == "sqlite" and not os.path.isfile(self._filepath):
            return False

        study_names = optuna.study.get_all_study_names(storage=self._storage)
        return path in study_names

    def _study_name_glob(self, pattern):
        study_names = optuna.study.get_all_study_names(storage=self._storage)
        for study_name in study_names:
            if fnmatch.fnmatch(study_name, pattern):
                yield study_name
