import json
import logging

from azure.monitor.opentelemetry.exporter import AzureMonitorLogExporter
from opentelemetry._logs import Logger, LogRecord, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from agents.errors import InvocationFailure, Metadata, failure_message

logger = logging.getLogger(__name__)


class ExceptionTelemetry:
    def __init__(self, connection_string: str | None, agent: str):
        self.provider: LoggerProvider | None = None
        self.logger: Logger | None = None
        if connection_string:
            self.provider = LoggerProvider(
                resource=Resource.create({"service.name": f"ai-cost-control-{agent}"})
            )
            exporter = AzureMonitorLogExporter(
                connection_string=connection_string, disable_offline_storage=True
            )
            self.provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
            self.logger = self.provider.get_logger("agents.exceptions")
        else:
            logger.warning(
                "Application Insights exception export disabled: "
                "APPLICATIONINSIGHTS_CONNECTION_STRING is not configured"
            )

    def record_failure(self, metadata: Metadata) -> None:
        record_failure(metadata, self.logger)

    def close(self) -> None:
        if self.provider is not None:
            if not self.provider.force_flush(timeout_millis=5000):
                logger.error("Application Insights exception telemetry did not flush within 5s")
            self.provider.shutdown()


def record_failure(metadata: Metadata, telemetry_logger: Logger | None = None) -> None:
    safe_error = InvocationFailure(failure_message(metadata))
    record = logger.makeRecord(
        logger.name,
        logging.ERROR,
        __file__,
        0,
        json.dumps(metadata),
        (),
        (InvocationFailure, safe_error, None),
        extra=metadata,
    )
    logger.handle(record)
    if telemetry_logger is not None:
        telemetry_logger.emit(
            LogRecord(
                body=failure_message(metadata),
                severity_number=SeverityNumber.ERROR,
                severity_text="ERROR",
                attributes={
                    **metadata,
                    "exception.type": "agents.errors.InvocationFailure",
                    "exception.message": failure_message(metadata),
                },
            )
        )
