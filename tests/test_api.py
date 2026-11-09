import json,os,unittest
from argparse import Namespace
from unittest.mock import MagicMock,patch
from momni.prescreen.client import Client


class APIContractTests(unittest.TestCase):
    def args(self,**kwargs):
        values=dict(model='provider-model',base_url='https://provider.invalid/v1',api_format='chat-completions',
            api_key_env='SCREENING_API_KEY',stream=False,response_format='none',max_tokens_field='max_tokens',
            max_tokens=1000,thinking=None,timeout=10,extra_body={},proxy_mode='system',auth_style='bearer',api_version='2026-10-21')
        values.update(kwargs);return Namespace(**values)
    def request(self,args,response):
        fake=MagicMock();fake.__enter__.return_value=MagicMock()
        fake.__enter__.return_value.read.return_value=json.dumps(response).encode()
        with patch.dict(os.environ,{'SCREENING_API_KEY':'contract-test-key'}),patch('urllib.request.urlopen',return_value=fake) as call:
            result=Client(args).request('system',{'question':'test'})
        req=call.call_args.args[0]
        return req,json.loads(req.data),result
    def test_generic_chat_has_no_vendor_fields(self):
        req,body,result=self.request(self.args(),{'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
        self.assertEqual(req.full_url,'https://provider.invalid/v1/chat/completions')
        self.assertNotIn('thinking',body);self.assertNotIn('response_format',body);self.assertNotIn('stream_options',body)
        self.assertFalse(body['stream']);self.assertEqual(req.get_header('Authorization'),'Bearer contract-test-key')
    def test_full_endpoint_and_model_specific_options(self):
        req,body,_=self.request(self.args(base_url='https://provider.invalid/v1/chat/completions',max_tokens_field='max_completion_tokens',response_format='json-object',extra_body={'reasoning_effort':'low'}),{'choices':[]})
        self.assertEqual(req.full_url,'https://provider.invalid/v1/chat/completions')
        self.assertEqual(body['max_completion_tokens'],1000);self.assertNotIn('max_tokens',body)
        self.assertEqual(body['reasoning_effort'],'low')
    def test_native_messages_headers_and_response_mapping(self):
        req,body,result=self.request(self.args(api_format='anthropic-messages',auth_style='x-api-key'),
            {'model':'native-model','stop_reason':'end_turn','content':[{'type':'text','text':'{"ok":true}'}],
             'usage':{'input_tokens':10,'output_tokens':4,'cache_read_input_tokens':5}})
        self.assertEqual(req.full_url,'https://provider.invalid/v1/messages')
        self.assertEqual(req.get_header('X-api-key'),'contract-test-key');self.assertNotIn('response_format',body)
        self.assertEqual(body['system'],'system');self.assertEqual(len(body['messages']),1)
        self.assertEqual(result['choices'][0]['finish_reason'],'stop')
        self.assertEqual(result['usage']['cache_read_input_tokens'],5)

    def test_stream_usage_can_be_omitted_and_json_fallback_parsed(self):
        args=self.args(stream=True,stream_usage=False)
        response={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        fake=MagicMock();fake.__enter__.return_value=MagicMock()
        fake.__enter__.return_value.headers={'Content-Type':'application/json'}
        fake.__enter__.return_value.read.return_value=json.dumps(response).encode()
        with patch.dict(os.environ,{'SCREENING_API_KEY':'contract-test-key'}),patch('urllib.request.urlopen',return_value=fake) as call:
            result=Client(args).request('test',{'question':'test'})
        body=json.loads(call.call_args.args[0].data)
        self.assertNotIn('stream_options',body)
        self.assertEqual(result['choices'][0]['message']['content'],'{}')

    def test_local_no_auth_does_not_require_or_send_key(self):
        args=self.args(base_url='http://127.0.0.1:8000/v1',auth_style='none')
        fake=MagicMock();fake.__enter__.return_value.read.return_value=json.dumps({'choices':[]}).encode()
        with patch.dict(os.environ,{},clear=True),patch('urllib.request.urlopen',return_value=fake) as call:
            Client(args).request('test',{})
        req=call.call_args.args[0]
        self.assertIsNone(req.get_header('Authorization'));self.assertIsNone(req.get_header('X-api-key'))


if __name__=='__main__':unittest.main()
