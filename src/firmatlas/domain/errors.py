"""FirmAtlas 异常层次（接口设计 §9）。

只定义当前功能用到的部分，其余异常随对应功能补充。
"""


class FirmAtlasError(Exception):
    """所有 FirmAtlas 自定义异常的基类。"""


class RepositoryError(FirmAtlasError):
    """基础设施故障。业务层永远不接触原始 SQLAlchemy/SQLite 异常。"""


class DatabaseNotInitializedError(RepositoryError):
    """数据库文件不存在，需要先运行 firmatlas init。"""


class SchemaVersionMismatchError(RepositoryError):
    """数据库结构版本与程序期望不一致，拒绝打开（AC-32）。"""
