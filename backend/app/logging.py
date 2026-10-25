import logging

import structlog


def setup_logging(level: str, *, json: bool) -> None:
    """Логи structlog: JSON на сервере, читаемый вывод локально."""
    logging.basicConfig(level=level.upper(), format="%(message)s")
    # Каждый запрос к hh.ru раз в минуту — шум; ошибки всё равно логируем сами
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # arq ставит свой обработчик; без этого его строки дублируются через root
    logging.getLogger("arq").propagate = False
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
