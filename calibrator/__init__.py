"""Visual calibration extension points."""

from calibrator.calibration_loop import CalibrationLoop
from calibrator.visual_checker import DeterministicVLMClient, VisualSelfCalibrator

__all__ = ["CalibrationLoop", "DeterministicVLMClient", "VisualSelfCalibrator"]
