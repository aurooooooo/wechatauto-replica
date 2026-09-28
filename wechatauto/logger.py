from .param import WxParam

import logging
import colorama
import os
from pathlib import Path
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler


colorama.init()

LOG_COLORS = {
    'DEBUG': colorama.Fore.CYAN,
    'INFO': colorama.Fore.GREEN,
    'WARNING': colorama.Fore.YELLOW,
    'ERROR': colorama.Fore.RED,
    'CRITICAL': colorama.Fore.MAGENTA
}


LOG_RETENTION_DAYS = 60
DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class WeeklyFileHandler(TimedRotatingFileHandler):
    """按周轮转，并清理超过保留期限的历史日志。"""

    def __init__(self, filename, retention_days=LOG_RETENTION_DAYS, **kwargs):
        self.retention_days = retention_days
        self.log_dir = Path(filename).resolve().parent
        super().__init__(
            filename,
            when="W0",
            interval=1,
            backupCount=0,
            encoding="utf-8",
            delay=True,
            **kwargs,
        )
        self.cleanup_old_logs()

    def doRollover(self):
        super().doRollover()
        self.cleanup_old_logs()

    def cleanup_old_logs(self):
        cutoff = datetime.now().timestamp() - self.retention_days * 24 * 60 * 60
        prefix = Path(self.baseFilename).name + "."
        for path in self.log_dir.glob(prefix + "*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                # 日志清理失败不能影响主程序运行。
                continue


class ColoredFormatter(logging.Formatter):
    def format(self, record):
        levelname = record.levelname
        message = super().format(record)
        return f"{LOG_COLORS[levelname]}{message}{colorama.Style.RESET_ALL}"

class WechatautoLogger:
    name: str = 'wechatauto'

    def __init__(self):
        self.logger = self.setup_logger()
        self.file_handler = None
        self.set_debug(False)

    def setup_logger(self) -> logging.Logger:
        """设置日志记录器"""
        # 配置根记录器
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        # 添加asyncio日志过滤
        logging.getLogger('asyncio').setLevel(logging.WARNING)

        # 设置第三方库的日志级别
        logging.getLogger('comtypes').setLevel(logging.WARNING)
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)

        # 清除现有处理器
        root_logger.handlers.clear()

        # 格式
        fmt = '%(asctime)s [%(name)s] [%(levelname)s] [%(filename)s:%(lineno)d]  %(message)s'

        # 控制台处理器（带颜色）
        self.console_handler = logging.StreamHandler()
        console_formatter = ColoredFormatter(
            fmt=fmt,
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.console_handler.setFormatter(console_formatter)
        self.console_handler.setLevel(logging.DEBUG)

        root_logger.addHandler(self.console_handler)

        return logging.getLogger(self.name)

    def setup_file_logger(self):
        """创建固定目录下的按周轮转文件日志处理器。"""
        if not WxParam.ENABLE_FILE_LOGGER or self.file_handler is not None:
            return

        log_dir = Path(os.environ.get("WECHATAUTO_LOG_DIR") or DEFAULT_LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "wechatauto.log"

        self.file_handler = WeeklyFileHandler(log_file)
        file_formatter = logging.Formatter(
            '%(asctime)s [%(name)s] [%(levelname)s] [%(filename)s:%(lineno)d]  %(message)s',
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.file_handler.setFormatter(file_formatter)
        self.file_handler.setLevel(logging.DEBUG)

        # 将文件处理器添加到日志记录器
        logging.getLogger().addHandler(self.file_handler)

    def set_debug(self, debug=False):
        """动态设置日志级别"""
        if debug:
            self.console_handler.setLevel(logging.DEBUG)
        else:
            self.console_handler.setLevel(logging.INFO)

    def _ensure_file_logger(self):
        """确保文件日志处理器被初始化"""
        if WxParam.ENABLE_FILE_LOGGER and self.file_handler is None:
            self.setup_file_logger()

    def debug(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.debug(msg, *args, stacklevel=stacklevel, **kwargs)

    def info(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.info(msg, *args, stacklevel=stacklevel, **kwargs)

    def warning(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.warning(msg, *args, stacklevel=stacklevel, **kwargs)

    def error(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.error(msg, *args, stacklevel=stacklevel, **kwargs)

    def critical(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.critical(msg, *args, stacklevel=stacklevel, **kwargs)

# 文件日志在第一次写入时初始化，避免导入模块时创建目录。
wxlog = WechatautoLogger()
