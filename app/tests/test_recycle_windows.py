"""Actual recoverable operation on one newly created, gated binary fixture only."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
from core import actions as A, memory as M


def main():
    gate = Path(os.environ['VCS_TEST_GATE'])
    end = time.monotonic() + 10
    while not gate.exists():
        if time.monotonic() > end: raise SystemExit(125)
        time.sleep(.02)
    output = Path(os.environ['VCS_REVIEW_OUTPUT'])
    memory = M.machine_memory()
    admission = M.admission(memory, 0, 512)
    if not admission['ok'] or not memory.get('commit_limit_mb'):
        print(json.dumps({'deferred': True, 'reason': admission['reason']})); return 77
    source = output / ('vcs_owned_recycle_' + uuid.uuid4().hex + '.bin')
    payload = b'VCS isolated recoverability test\n' + uuid.uuid4().hex.encode() * 8
    source.write_bytes(payload)
    keeper = output / 'retained_sentinel.bin'; keeper.write_bytes(b'keep this own sentinel')
    original = hashlib.sha256(keeper.read_bytes()).hexdigest()
    bin_root = Path(source.anchor) / '$Recycle.Bin'
    allowed, why = A.recycle_bin_available(str(source))
    assert allowed, why
    protected = output / ('.vcs-recovery-' + uuid.uuid4().hex)
    result = A.recycle(str(source), recovery_path=str(protected), expected_identity=A.file_identity(str(source)))
    if os.environ.get('VCS_RECYCLE_EXPECT_RETAIN') == '1':
        assert not result['ok'], 'restricted profile should refuse unverifiable recycling'
        assert source.read_bytes() == payload, 'a refused recycle must preserve the original bytes'
        assert hashlib.sha256(keeper.read_bytes()).hexdigest() == original
        assert not list(output.glob('.vcs-recovery-*'))
        summary = {'passed': True, 'checks': 4, 'expected_safe_refusal': True,
                   'source': str(source), 'source_sha256': hashlib.sha256(payload).hexdigest(),
                   'recycle_result': result, 'no_video_or_queue': True}
        (output / 'recycle_checks.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(summary, ensure_ascii=False)); return 0
    assert result['ok'], result
    assert not source.exists(), 'source should have moved to the bin'
    recycled = Path(result['recycled_path'])
    assert recycled.name.startswith('$R') and recycled.parent.parent == bin_root
    info = recycled.with_name('$I' + recycled.name[2:])
    encoded_path = str(source).encode('utf-16le')
    assert info.is_file() and encoded_path in info.read_bytes()
    assert recycled.is_file() and recycled.read_bytes() == payload
    assert hashlib.sha256(keeper.read_bytes()).hexdigest() == original
    missing = A.recycle(str(output / 'owned_missing.bin'))
    assert not missing['ok']
    summary = {'passed': True, 'checks': 5, 'source': str(source), 'recoverable_record': str(info),
               'keeper_sha256': original, 'recycle_result': result,
               'owned_synthetic_bytes': len(payload), 'no_video_or_queue': True}
    (output / 'recycle_checks.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
