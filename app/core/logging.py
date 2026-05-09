import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure structured-enough console logging for containers."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
