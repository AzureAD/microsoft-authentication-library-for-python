#------------------------------------------------------------------------------
#
# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files(the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and / or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions :
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
#------------------------------------------------------------------------------

try:
    from xml.etree import cElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET
import os

from msal.wstrust_response import *

from tests import unittest


class Test_WsTrustResponse(unittest.TestCase):

    def test_findall_content_with_comparison(self):
        content = """
            <saml:Assertion xmlns:saml="SAML:assertion">
                <ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
                foo
                </ds:Signature>
            </saml:Assertion>"""
        sample = ('<ns0:Wrapper xmlns:ns0="namespace0">'
            + content
            + '</ns0:Wrapper>')

        # Demonstrating how XML-based parser won't give you the raw content as-is
        element = ET.fromstring(sample).findall('{SAML:assertion}Assertion')[0]
        assertion_via_xml_parser = ET.tostring(element)
        self.assertNotEqual(content, assertion_via_xml_parser)
        self.assertNotIn(b"<ds:Signature>", assertion_via_xml_parser)

        # The findall_content() helper, based on Regex, will return content as-is.
        self.assertEqual([content], findall_content(sample, "Wrapper"))

    def test_parse_error(self):
        error_response = '''
            <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:u="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
              <s:Header>
               <a:Action s:mustUnderstand="1">http://www.w3.org/2005/08/addressing/soap/fault</a:Action>
               <o:Security s:mustUnderstand="1" xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
                 <u:Timestamp u:Id="_0">
                 <u:Created>2013-07-30T00:32:21.989Z</u:Created>
                 <u:Expires>2013-07-30T00:37:21.989Z</u:Expires>
                 </u:Timestamp>
               </o:Security>
               </s:Header>
             <s:Body>
               <s:Fault>
                 <s:Code>
                   <s:Value>s:Sender</s:Value>
                   <s:Subcode>
                   <s:Value xmlns:a="http://docs.oasis-open.org/ws-sx/ws-trust/200512">a:RequestFailed</s:Value>
                   </s:Subcode>
                 </s:Code>
                 <s:Reason>
                 <s:Text xml:lang="en-US">MSIS3127: The specified request failed.</s:Text>
                 </s:Reason>
               </s:Fault>
            </s:Body>
            </s:Envelope>'''
        self.assertEqual({
            "reason": "MSIS3127: The specified request failed.",
            "code": "a:RequestFailed",
            }, parse_error(error_response))

    def test_token_parsing_happy_path(self):
        with open(os.path.join(os.path.dirname(__file__), "rst_response.xml")) as f:
            rst_body = f.read()
        result = parse_token_by_re(rst_body)
        self.assertEqual(result.get("type"), SAML_TOKEN_TYPE_V1)
        self.assertIn(b"<saml:Assertion", result.get("token", ""))

