# -*- coding: utf-8 -*-
PWFILE = "tests/testdata/passwords"

from .base import MyTestCase, FakeFlaskG
from privacyidea.lib.error import ParameterError, PolicyError
from privacyidea.lib.resolver import (save_resolver)
from privacyidea.lib.realm import (set_realm)
from privacyidea.lib.user import (User)
from privacyidea.lib.tokenclass import DATE_FORMAT
from privacyidea.lib.utils import b32encode_and_unicode
from privacyidea.lib.tokens.pushtoken import PushTokenClass, PUSH_ACTION, DEFAULT_CHALLENGE_TEXT
from privacyidea.lib.smsprovider.FirebaseProvider import FIREBASE_CONFIG
from privacyidea.lib.token import get_tokens, remove_token
from privacyidea.lib.challenge import get_challenges
from privacyidea.models import (Token,
                                 Config,
                                 Challenge)
from privacyidea.lib.config import (set_privacyidea_config, set_prepend_pin)
from privacyidea.lib.policy import (PolicyClass, SCOPE, set_policy,
                                    delete_policy)
from privacyidea.lib.smsprovider.SMSProvider import set_smsgateway

import binascii
import datetime
import hashlib
import base64
from dateutil.tz import tzlocal
import json

from passlib.utils.pbkdf2 import pbkdf2


class PushTokenTestCase(MyTestCase):

    serial1 = "PUSH00001"

    def test_01_create_token(self):
        db_token = Token(self.serial1, tokentype="push")
        db_token.save()
        token = PushTokenClass(db_token)
        self.assertEqual(token.token.serial, self.serial1)
        self.assertEqual(token.token.tokentype, "push")
        self.assertEqual(token.type, "push")
        class_prefix = token.get_class_prefix()
        self.assertEqual(class_prefix, "PIPU")
        self.assertEqual(token.get_class_type(), "push")

        # Test to do the 2nd step, although the token is not yet in clientwait
        self.assertRaises(ParameterError, token.update, {"otpkey": "1234", "pubkey": "1234", "serial": self.serial1})

        # Run enrollment step 1
        token.update({"genkey": 1})

        # Now the token is in the state clientwait, but insufficient parameters would still fail
        self.assertRaises(ParameterError, token.update, {"otpkey": "1234"})
        self.assertRaises(ParameterError, token.update, {"otpkey": "1234", "pubkey": "1234"})

        # Unknown config
        self.assertRaises(ParameterError, token.get_init_detail, params={"firebase_config": "bla"})

        r = set_smsgateway("fb1", u'privacyidea.lib.smsprovider.FirebaseProvider.FirebaseProvider', "myFB",
                           {FIREBASE_CONFIG.REGISTRATION_URL: "http://test/ttype/push",
                            FIREBASE_CONFIG.TTL: 10,
                            FIREBASE_CONFIG.API_KEY: "1",
                            FIREBASE_CONFIG.APP_ID: "2",
                            FIREBASE_CONFIG.PROJECT_NUMBER: "3",
                            FIREBASE_CONFIG.PROJECT_ID: "4"})
        self.assertTrue(r > 0)

        detail = token.get_init_detail(params={"firebase_config": "fb1"})
        self.assertEqual(detail.get("serial"), self.serial1)
        self.assertEqual(detail.get("rollout_state"), "clientwait")
        enrollment_credential = detail.get("enrollment_credential")
        self.assertTrue("pushurl" in detail)
        self.assertFalse("otpkey" in detail)

        # Run enrollment step 2
        token.update({"enrollment_credential": enrollment_credential,
                      "serial": self.serial1,
                      "fbtoken": "firebasetoken",
                      "pubkey": "pubkey"})
        self.assertEqual(token.get_tokeninfo("firebase_token"), "firebasetoken")
        self.assertEqual(token.get_tokeninfo("public_key_smartphone"), "pubkey")
        self.assertTrue(token.get_tokeninfo("public_key_server").startswith("-----BEGIN RSA PUBLIC KEY-----"),
                        token.get_tokeninfo("public_key_server"))

        detail = token.get_init_detail()
        self.assertEqual(detail.get("rollout_state"), "enrolled")
        self.assertTrue(detail.get("public_key").startswith("MII"))
        remove_token(self.serial1)

    def test_02_api_enroll(self):
        self.authenticate()

        # Failed enrollment due to missing policy
        with self.app.test_request_context('/token/init',
                                           method='POST',
                                           data={"type": "push",
                                                 "genkey": 1},
                                           headers={'Authorization': self.at}):
            res = self.app.full_dispatch_request()
            self.assertNotEqual(res.status_code,  200)
            error = json.loads(res.data.decode("utf8")).get("result").get("error")
            self.assertEqual(error.get("message"), "Missing enrollment policy for push token: push_firebase_configuration")
            self.assertEqual(error.get("code"), 303)

        r = set_smsgateway("fb1", u'privacyidea.lib.smsprovider.FirebaseProvider.FirebaseProvider', "myFB",
                           {FIREBASE_CONFIG.REGISTRATION_URL: "http://test/ttype/push",
                            FIREBASE_CONFIG.TTL: 10,
                            FIREBASE_CONFIG.API_KEY: "1",
                            FIREBASE_CONFIG.APP_ID: "2",
                            FIREBASE_CONFIG.PROJECT_NUMBER: "3",
                            FIREBASE_CONFIG.PROJECT_ID: "4"})
        self.assertTrue(r > 0)
        set_policy("push1", scope=SCOPE.ENROLL,
                   action="{0!s}=fb1".format(PUSH_ACTION.FIREBASE_CONFIG))

        # 1st step
        with self.app.test_request_context('/token/init',
                                           method='POST',
                                           data={"type": "push",
                                                 "genkey": 1},
                                           headers={'Authorization': self.at}):
            res = self.app.full_dispatch_request()
            self.assertEqual(res.status_code,  200)
            detail = json.loads(res.data.decode('utf8')).get("detail")
            serial = detail.get("serial")
            self.assertEqual(detail.get("rollout_state"), "clientwait")
            self.assertTrue("pushurl" in detail)
            self.assertFalse("otpkey" in detail)
            enrollment_credential = detail.get("enrollment_credential")

        # 2nd step. Failing with wrong serial number
        with self.app.test_request_context('/ttype/push',
                                           method='POST',
                                           data={"serial": "wrongserial",
                                                 "pubkey": "pubkey",
                                                 "fbtoken": "firebaseT"}):
            res = self.app.full_dispatch_request()
            self.assertTrue(res.status_code == 400, res)
            status = json.loads(res.data.decode('utf8')).get("result").get("status")
            self.assertFalse(status)
            error = json.loads(res.data.decode('utf8')).get("result").get("error")
            self.assertEqual(error.get("message"),
                             "ERR905: No token with this serial number in the rollout state 'clientwait'.")

        # 2nd step. Fails with missing enrollment credential
        with self.app.test_request_context('/ttype/push',
                                           method='POST',
                                           data={"serial": serial,
                                                 "pubkey": "pubkey",
                                                 "fbtoken": "firebaseT"}):
            res = self.app.full_dispatch_request()
            self.assertTrue(res.status_code == 400, res)
            status = json.loads(res.data.decode('utf8')).get("result").get("status")
            self.assertFalse(status)
            error = json.loads(res.data.decode('utf8')).get("result").get("error")
            self.assertEqual(error.get("message"),
                             "ERR905: Invalid enrollment credential. You are not authorized to finalize this token.")

        # 2nd step: as performed by the smartphone
        with self.app.test_request_context('/ttype/push',
                                           method='POST',
                                           data={"enrollment_credential": enrollment_credential,
                                                 "serial": serial,
                                                 "pubkey": "pubkey",
                                                 "fbtoken": "firebaseT"}):
            res = self.app.full_dispatch_request()
            self.assertTrue(res.status_code == 200, res)
            detail = json.loads(res.data.decode('utf8')).get("detail")
            # still the same serial number
            self.assertEqual(serial, detail.get("serial"))
            self.assertEqual(detail.get("rollout_state"), "enrolled")
            # Now the smartphone gets a public key from the server
            self.assertTrue(detail.get("public_key").startswith("MII"))
            pubkey = detail.get("public_key")

            # Now check, what is in the token in the database
            toks = get_tokens(serial=serial)
            self.assertEqual(len(toks), 1)
            token_obj = toks[0]
            self.assertEqual(token_obj.token.rollout_state, u"enrolled")
            self.assertTrue(token_obj.token.active)
            tokeninfo = token_obj.get_tokeninfo()
            self.assertEqual(tokeninfo.get("public_key_smartphone"), u"pubkey")
            self.assertEqual(tokeninfo.get("firebase_token"), u"firebaseT")
            # The private key of the server is stored in the otpkey
            self.assertEqual(tokeninfo.get("public_key_server").strip().strip("-BEGIN END RSA PUBLIC KEY-").strip(), pubkey)
            # The token should also contain the firebase config
            self.assertEqual(tokeninfo.get(PUSH_ACTION.FIREBASE_CONFIG), "fb1")

    def test_03_api_authenticate(self):
        self.setUp_user_realms()

        # get enrolled push token
        toks = get_tokens(tokentype="push")
        self.assertEqual(len(toks), 1)
        tokenobj = toks[0]
        transaction_id = None

        # set PIN
        tokenobj.set_pin("pushpin")
        tokenobj.add_user(User("cornelius", self.realm1))

        # Send the first authentication request to trigger the challenge
        with self.app.test_request_context('/validate/check',
                                           method='POST',
                                           data={"user": "cornelius",
                                                 "realm": self.realm1,
                                                 "pass": "pushpin"}):
            res = self.app.full_dispatch_request()
            self.assertTrue(res.status_code == 200, res)
            jsonresp = json.loads(res.data.decode('utf8'))
            self.assertFalse(jsonresp.get("result").get("value"))
            self.assertTrue(jsonresp.get("result").get("status"))
            self.assertEqual(jsonresp.get("detail").get("serial"), tokenobj.token.serial)
            self.assertTrue("transaction_id" in jsonresp.get("detail"))
            transaction_id = jsonresp.get("detail").get("transaction_id")
            self.assertEqual(jsonresp.get("detail").get("message"), DEFAULT_CHALLENGE_TEXT)

        # The mobile device has not communicated with the backend, yet.
        # The user is not authenticated!
        with self.app.test_request_context('/validate/check',
                                           method='POST',
                                           data={"user": "cornelius",
                                                 "realm": self.realm1,
                                                 "pass": "",
                                                 "transaction_id": transaction_id}):
            res = self.app.full_dispatch_request()
            self.assertTrue(res.status_code == 200, res)
            jsonresp = json.loads(res.data.decode('utf8'))
            # Result-Value is false, the user has not answered the challenge, yet
            self.assertFalse(jsonresp.get("result").get("value"))

        # Now the smartphone communicates with the backend and the challenge in the database table
        # is marked as answered successfully.
        challengeobject_list = get_challenges(serial=tokenobj.token.serial,
                                              transaction_id=transaction_id)
        challengeobject_list[0].set_otp_status(True)

        with self.app.test_request_context('/validate/check',
                                           method='POST',
                                           data={"user": "cornelius",
                                                 "realm": self.realm1,
                                                 "pass": "",
                                                 "state": transaction_id}):
            res = self.app.full_dispatch_request()
            self.assertTrue(res.status_code == 200, res)
            jsonresp = json.loads(res.data.decode('utf8'))
            # Result-Value is false, the user has not answered the challenge, yet
            self.assertTrue(jsonresp.get("result").get("value"))

