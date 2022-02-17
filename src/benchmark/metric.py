from abc import ABC
from typing import List, Dict
from math import log

from common.statistic import Stat, merge_stat
from common.object_spec import ObjectSpec, create_object
from common.general import singleton
from .adapter import AdapterSpec, ScenarioState, RequestState
from .metric_service import MetricService
from .scenario import VALID_TAG, TEST_TAG


class Metric(ABC):
    """
    A `Metric` takes the results of execution and produces `Stat`s for a
    scenario.

    Note: `Metric` actually right now is a bit of misnomer because it produces many
    `Stat`s, that might be distinct but are computed together.  Eventually we
    might move to a world where there is one (or very few metrics that are domain-independent).
    """

    def evaluate(self, scenario_state: ScenarioState, metric_service: MetricService) -> List[Stat]:
        """
        Main entry point for a `Metric`.  This function groups the single
        list of `RequestState` by training trial and instance, and invokes
        other functions to process those.  This should serve most purposes.

        Any logic that doesn't decompose along instances should go here, such
        as robustness.
        """
        adapter_spec = scenario_state.adapter_spec
        global_stats: Dict[str, Stat] = {}  # name -> Stat

        for train_trial_index in range(adapter_spec.num_train_trials):
            trial_stats: Dict[str, Stat] = {}  # Statistics just for this trial
            # TODO: incorporate robustness (worst case over a group of instances with some tag)
            #       https://github.com/stanford-crfm/benchmarking/issues/47
            # TODO: incorporate disparities (compute difference between average over instances with some tag)
            #       https://github.com/stanford-crfm/benchmarking/issues/48
            for instance in scenario_state.instances:
                instance_stats = []

                # Evaluate generated request_state
                request_state = singleton(scenario_state.get_request_states(train_trial_index, instance, None))
                instance_stats.extend(self.evaluate_generation(adapter_spec, request_state, metric_service))

                # Evaluate the references
                request_states = []
                for reference_index in range(len(instance.references)):
                    request_states.extend(
                        scenario_state.get_request_states(train_trial_index, instance, reference_index)
                    )
                instance_stats.extend(self.evaluate_references(adapter_spec, request_states, metric_service))

                # Merge these statistics back.
                # TODO: we should add statistics with the individual instances too and serialize them out.
                #       https://github.com/stanford-crfm/benchmarking/issues/49
                for stat in instance_stats:
                    if VALID_TAG in instance.tags:
                        stat = Stat(name=VALID_TAG + "." + stat.name).merge(stat)
                    elif TEST_TAG in instance.tags:
                        stat = Stat(name=TEST_TAG + "." + stat.name).merge(stat)
                    merge_stat(trial_stats, stat)

            # Aggregate the corpus-level metrics
            for tag in [VALID_TAG, TEST_TAG]:
                if tag + "." + "logprob" in trial_stats and tag + "." + "num_tokens" in trial_stats:
                    merge_stat(
                        trial_stats,
                        Stat(tag + "." + "perplexity").add(
                            2 ** (-trial_stats[tag + "." + "logprob"].sum / trial_stats[tag + "." + "num_tokens"].sum)
                        ),
                    )
                    merge_stat(
                        trial_stats,
                        Stat(tag + "." + "bits_per_byte").add(
                            -trial_stats[tag + "." + "logprob"].sum / trial_stats[tag + "." + "num_bytes"].sum / log(2)
                        ),
                    )

            # We only take the mean value for each trial
            for stat in trial_stats.values():
                merge_stat(global_stats, stat.take_mean())

        return list(global_stats.values())

    def evaluate_generation(
        self, adapter_spec: AdapterSpec, request_state: RequestState, metric_service: MetricService
    ) -> List[Stat]:
        """Evaluate free-form generation.  Override me!"""
        return []

    def evaluate_references(
        self, adapter_spec: AdapterSpec, reference_request_states: List[RequestState], metric_service: MetricService
    ) -> List[Stat]:
        """Evaluate the references.  Override me!"""
        return []


class MetricSpec(ObjectSpec):
    """Specifies how to create a `Metric`."""

    pass


def create_metric(metric_spec: MetricSpec) -> Metric:
    return create_object(metric_spec)