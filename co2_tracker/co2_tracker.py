""" Contains implementations of the Public facing API: CO2Tracker, OfflineCO2Tracker and @track_co2 """

import logging
import os
import time
import uuid
from abc import abstractmethod, ABC
from datetime import datetime
from functools import wraps
from typing import Optional, List, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from co2_tracker_utils.gpu_logging import is_gpu_details_available

from co2_tracker.config import cfg, AppConfig
from co2_tracker.emissions import (
    get_cloud_emissions,
    get_private_infra_emissions,
    get_cloud_country,
)
from co2_tracker.external.geography import GeoMetadata, CloudMetadata
from co2_tracker.external.hardware import GPU, CPU
from co2_tracker.persistence import (
    FilePersistence,
    CO2Data,
    BasePersistence,
)
from co2_tracker.units import Time, Energy, Power

logger = logging.getLogger(__name__)


class BaseCO2Tracker(ABC):
    """ Primary abstraction with the CO2 Tracker functionality.
     Has two abstract methods, `_get_geo_metadata` and `_get_cloud_metadata` that are implemented by the
     two concrete classes `OfflineCO2Tracker` and `CO2Tracker.`
    """

    def __init__(
        self,
        project_name: str = "default",
        measure_power_secs: int = 15,
        output_dir: str = ".",
        save_to_file: bool = True,
    ):
        """

        Args:
            project_name (str): Project name for current experiment run. Default value of "default"
            measure_power_secs (int): Interval in seconds in which the GPU power is measured.
                                                       Defaults to 15.
            output_dir (str): Directory path to which the experiment artifacts are saved. Saved to current directory
                              by default.
            save_to_file (bool): Indicates if the emission artifacts should be logged to a file
        """
        self._project_name: str = project_name
        self._measure_power_secs: int = measure_power_secs
        self._start_time: Optional[float] = None
        self._output_dir: str = output_dir
        self._total_energy: Energy = Energy.from_energy(kwh=0)
        self._scheduler = BackgroundScheduler()
        self._is_gpu_available = is_gpu_details_available()
        self._hardware = (
            GPU.from_co2_tracker_utils()
        )  # TODO: Change once CPU support is available

        # Run `self._measure_power` every `measure_power_secs` seconds in a background thread:
        self._scheduler.add_job(
            self._measure_power, "interval", seconds=measure_power_secs,
        )

        self._app_config: AppConfig = self._get_config()
        self.persistence_objs: List[BasePersistence] = list()

        if save_to_file:
            self.persistence_objs.append(
                FilePersistence(os.path.join(output_dir, f"{project_name}.emissions"))
            )

    def start(self) -> None:
        """ Starts tracking the experiment. Currently, Nvidia GPUs are supported.
        
        Returns: None
        """

        # TODO: Change once CPU support is available
        if not self._is_gpu_available:
            logger.warning("No GPU available")
            return

        if self._start_time is not None:
            logger.warning("Already started tracking")
            return

        self._start_time = time.time()
        self._scheduler.start()

    def stop(self) -> Optional[float]:
        """ Stops tracking the experiment.

        Returns: CO2 emissions in kgs.
        """
        if self._start_time is None:
            logging.error("Need to first start the tracker")
            return None

        self._scheduler.shutdown()

        cloud: CloudMetadata = self._get_cloud_metadata()
        geo: GeoMetadata = self._get_geo_metadata()
        duration: Time = Time.from_seconds(time.time() - self._start_time)

        emissions: float = (
            get_private_infra_emissions(self._total_energy, geo, self._app_config)
            if cloud.is_on_private_infra
            else get_cloud_emissions(self._total_energy, cloud, self._app_config)
        )

        country: str = (
            geo.country
            if cloud.is_on_private_infra
            else get_cloud_country(cloud, self._app_config)
        )

        region: str = (
            ("" if geo.region is None else geo.region)
            if cloud.is_on_private_infra
            else ""
        )

        data = CO2Data(
            timestamp=datetime.now(),
            experiment_id=str(uuid.uuid4()),
            project_name=self._project_name,
            duration=duration.seconds,
            emissions=emissions,
            total_energy_usage=self._total_energy.kwh,
            country=country,
            region=region,
        )

        for persistence in self.persistence_objs:
            persistence.flush(data)

        return emissions

    @abstractmethod
    def _get_geo_metadata(self) -> GeoMetadata:
        """
        Returns: Metadata containing geographical info
        """
        pass

    @abstractmethod
    def _get_cloud_metadata(self) -> CloudMetadata:
        """
        Returns: Metadata containing cloud info
        """
        pass

    def _measure_power(self) -> None:
        """ A function that is periodically run by the `BackgroundScheduler`
        every `self._measure_power` seconds.

        Returns: None
        """
        self._total_energy += Energy.from_power_and_time(
            power=self._hardware.total_power,
            time=Time.from_seconds(self._measure_power_secs),
        )

    def _get_config(self) -> AppConfig:
        return AppConfig(cfg)


class OfflineCO2Tracker(BaseCO2Tracker):
    """ Offline implementation of the `CO2tracker.` In addition to the standard arguments, the following
    are required.
    """

    def __init__(self, country: str, *args, region: Optional[str] = None, **kwargs):
        """
        Args:
            country (str): The country in which the experiment in being run.
            region (str): The provincial region, for example, California in the US. Currently, this only affects
                          calculations for the United States.
        """
        # TODO: Currently we silently use a default value of Canada. Decide if we should fail with missing args.
        self._country: str = country if country is not None else "Canada"
        self._region: Optional[str] = region
        super().__init__(*args, **kwargs)

    def _get_geo_metadata(self) -> GeoMetadata:
        return GeoMetadata(country=self._country, region=self._region)

    def _get_cloud_metadata(self) -> CloudMetadata:
        return CloudMetadata(provider=None, region=None)


class CO2Tracker(BaseCO2Tracker):
    """ A CO2 tracker that auto infers geographical location.
    """

    def _get_geo_metadata(self) -> GeoMetadata:
        config: AppConfig = self._get_config()
        return GeoMetadata.from_geo_js(config.geo_js_url)

    def _get_cloud_metadata(self) -> CloudMetadata:
        return CloudMetadata.from_co2_tracker_utils()


def track_co2(
    fn: Callable = None,
    project_name: str = "default",
    output_dir: str = ".",
    offline: bool = False,
    country: Optional[str] = None,
    region: Optional[str] = None,
):
    """ Decorator that supports both `CO2Tracker` and `OfflineCO2Tracker`

    Args:
        fn: Function to be decorated
        project_name (str): Project name for current experiment run. Default value of "default"
        output_dir (str): Directory path to which the experiment artifacts are saved.
                          Saved to current directory by default.
        offline (bool): Indicates if the tracker should be run in offline mode.
        country (str): The country in which the experiment in being run. Required if `offline=True`
        region (str): The provincial region, for example, California in the US. Currently, this only affects
                      calculations for the United States.
    Returns: The decorated function
    """

    def _decorate(fn: Callable):
        @wraps(fn)
        def wrapped_fn(*args, **kwargs):
            if offline:
                if country is None:
                    raise Exception("Country is a required argument for Offline mode")
                tracker = OfflineCO2Tracker(
                    project_name=project_name,
                    output_dir=output_dir,
                    country=country,
                    region=region,
                )
                tracker.start()
                fn(*args, **kwargs)
                tracker.stop()
            else:
                tracker = CO2Tracker(project_name=project_name, output_dir=output_dir)
                tracker.start()
                fn(*args, **kwargs)
                tracker.stop()

        return wrapped_fn

    if fn:
        return _decorate(fn)
    return _decorate