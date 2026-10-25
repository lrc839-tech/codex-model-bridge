#!/usr/bin/env python3
"""Create a disposable, eight-check demo. This script never calls an API."""
from pathlib import Path
import json
import tempfile

root=Path(tempfile.mkdtemp(prefix='model-bridge-demo-')).resolve()
(root/'normalize.py').write_text('def normalize_name(value):\n    return value.strip()\n')
(root/'test_normalize.py').write_text('''import unittest
from normalize import normalize_name
class NormalizeTests(unittest.TestCase):
    def test_spaces(self): self.assertEqual(normalize_name('  Alice  '),'Alice')
    def test_internal(self): self.assertEqual(normalize_name(' A  B '),'A  B')
    def test_empty(self): self.assertEqual(normalize_name(''),'')
    def test_blank(self): self.assertEqual(normalize_name(' \\t\\n '),'')
    def test_unicode(self): self.assertEqual(normalize_name(' 张三 '),'张三')
    def test_none(self): self.assertEqual(normalize_name(None),'')
    def test_number(self): self.assertEqual(normalize_name(42),'')
    def test_object(self): self.assertEqual(normalize_name({'name':'A'}),'')
if __name__=='__main__': unittest.main()
''')
task={'workspace':str(root),'task':'修复 normalize_name：去掉字符串首尾空白，保留内部空白，非字符串返回空字符串。',
      'risk_level':'low','input_files':['test_normalize.py'],'writable_files':['normalize.py'],
      'verification_files':['test_normalize.py'],'acceptance':['内部空白保持原样','不修改测试'],
      'acceptance_checks':[{'name':'八项固定行为测试','argv':['python3','-m','unittest','test_normalize.py'],
                            'timeout_seconds':30,'success_contains':['Ran 8 tests','OK']}]}
path=root/'task.json'
path.write_text(json.dumps(task,ensure_ascii=False,indent=2))
print(json.dumps({'workspace':str(root),'task_file':str(path),'provider_called':False},ensure_ascii=False))
