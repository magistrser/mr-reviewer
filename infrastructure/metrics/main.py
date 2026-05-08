from prometheus_client import CollectorRegistry, Counter, Histogram


prometheus_registry = CollectorRegistry()

DURATION_SECONDS = Histogram(
    'mr_review_duration_seconds',
    'Time spent processing operation',
    ['target', 'operation'],
    registry=prometheus_registry,
)

ERRORS = Counter(
    'mr_review_errors_total',
    'Total number of errors occurred during operation',
    ['target', 'operation'],
    registry=prometheus_registry,
)
