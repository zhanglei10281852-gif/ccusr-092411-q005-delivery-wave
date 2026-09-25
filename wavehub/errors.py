"""业务异常类型。

所有由 WaveHub 抛出的业务异常都继承 :class:`WaveHubError`，
调用方可以按基类统一捕获，也可以按具体类型分别处理。
"""
from __future__ import annotations


class WaveHubError(Exception):
    """系统内所有业务异常的基类。"""


class NotFoundError(WaveHubError):
    """引用的实体不存在。"""


class StateError(WaveHubError):
    """实体当前所处的环节不允许执行该操作。"""


class CutoffError(StateError):
    """波次已截单，需求只能经由获批差异单变更。"""


class VersionMismatchError(StateError):
    """操作引用的配送版本与业务发生时的有效版本不一致。"""


class SealConflictError(StateError):
    """封签冲突：线路上已存在有效封签，并发封签只保留一个结果。"""


class InfeasiblePlanError(WaveHubError):
    """波次编排不可行，异常信息列出全部不可安置的需求及原因。"""


class TransferError(WaveHubError):
    """整车转派不满足约束（目标车辆不可用、载重或温层不符等）。"""
