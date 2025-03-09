import logging


logger = logging.getLogger(__name__)


def is_pymsalruntime_installed() -> bool:
    try:
        import pymsalruntime
        logger.info("PyMsalRuntime installed and initialized")
        return True
    except ImportError:
        logger.info("PyMsalRuntime not installed")
        return False
    except RuntimeError:
        logger.warning(
            "PyMsalRuntime installed but failed to initialize the real broker. "
            "This may happen on Mac and Linux where broker is not built-in. "
            "Test cases shall attempt broker and test its fallback behavior."
        )
        return True
