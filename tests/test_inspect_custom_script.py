import importlib.util
from pathlib import Path
import unittest

PATH=Path(__file__).resolve().parents[1]/'tools/inspect_custom_script.py'
SPEC=importlib.util.spec_from_file_location('inspect_custom_script',PATH)
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InspectorTests(unittest.TestCase):
    def test_hides_literals_numbers_and_comments_without_execution(self):
        source="""# COMMENT_PRIVATE_VALUE
TOKEN = 'PRIVATE_STRING_VALUE'
CHAT_ID = 987654321
raise RuntimeError('DO_NOT_EXECUTE')
"""
        output=MODULE.sanitized_source(source)
        for secret in ('COMMENT_PRIVATE_VALUE','PRIVATE_STRING_VALUE','987654321','DO_NOT_EXECUTE'):
            self.assertNotIn(secret,output)
        self.assertIn('raise RuntimeError',output)

    def test_preserves_known_database_and_output_paths(self):
        source="with open('/tmp/final_forecast.txt','w',encoding='utf-8') as f:\n f.write('PRIVATE_REPORT')"
        output=MODULE.sanitized_source(source)
        self.assertIn('/tmp/final_forecast.txt',output)
        self.assertNotIn('PRIVATE_REPORT',output)


if __name__=='__main__':
    unittest.main()
