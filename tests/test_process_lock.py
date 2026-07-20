"""数据目录进程锁测试。"""

import pytest

from firmatlas.domain.errors import ProcessLockError
from firmatlas.infra.process_lock import DataDirectoryLock


def test_data_directory_lock_prevents_concurrent_processes_and_releases(tmp_path):
    data_dir = tmp_path / "data"
    first = DataDirectoryLock(data_dir)
    second = DataDirectoryLock(data_dir)

    first.acquire()
    with pytest.raises(ProcessLockError, match="另一个 FirmAtlas 进程"):
        second.acquire()

    first.release()
    second.acquire()
    second.release()

    assert (data_dir / ".firmatlas.lock").is_file()
