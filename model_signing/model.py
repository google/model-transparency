# Copyright Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing perepo_managerissions and
# limitations under the License.

from sigstore.sign import SigningContext

from sigstore.oidc import (
    IdentityToken,
    ExpiredIdentity,
    Issuer,
    detect_credential,
)
from sigstore_protobuf_specs.dev.sigstore.bundle.v1 import Bundle
from sigstore.verify import (
    policy,
    Verifier,
)
from sigstore.verify.models import (
    VerificationMaterials,
)

from sigstore._internal.fulcio.client import (
    ExpiredCertificate,
)

import io
from pathlib import Path
from typing import Optional
from serialize import Serializer
import psutil
import sys

from _manifest import Manifest

def chunk_size() -> int:
    return int(psutil.virtual_memory().available // 2)


# TODO: Update this class to have a status instead of success.
class BaseResult:
    def __init__(self, success: bool = True, reason: str = "success"):
        self.success = success
        self.reason = reason

    def __bool__(self) -> bool:
        return self.success

    def __str__(self) -> str:
        return f"success=\"{self.success}\" reason=\"{self.reason}\""


class SignatureResult(BaseResult):
    pass


class SigstoreSigner():
    def __init__(self,
                 disable_ambient: bool = False,
                 start_default_browser: bool = False,
                 oidc_issuer: str = None):
        self.signing_ctx = SigningContext.production()
        self.disable_ambient = disable_ambient
        self.start_default_browser = start_default_browser
        self.oidc_issuer = oidc_issuer
        # NOTE: The client ID to use during OAuth2 flow.
        self.client_id = "sigstore"

    def get_identity_token(self) -> Optional[IdentityToken]:
        token: IdentityToken
        client_id = self.client_id
        if not self.disable_ambient:
            token = detect_credential()
            # Happy path: we've detected an ambient credential,
            # so we can return early.
            if token:
                return IdentityToken(token)

        # TODO(): Support staging for testing.
        if self.oidc_issuer is not None:
            issuer = Issuer(self.oidc_issuer)
        else:
            issuer = Issuer.production()

        token = issuer.identity_token(client_id=client_id,
                                      force_oob=not self.start_default_browser)
        return token

    # NOTE: Only path in the top-level folder are considered for ignorepaths.
    def sign(self, inputfn: Path, signaturefn: Path,
             ignorepaths: [Path]) -> SignatureResult:
        try:
            oidc_token = self.get_identity_token()
            if not oidc_token:
                raise ValueError("No identity token supplied or detected!")
            print(f"identity-provider: {oidc_token.issuer}",
                  file=sys.stderr)
            print(f"identity: {oidc_token.identity}", file=sys.stderr)

            serialized_paths = Serializer.serialize_v2(
                inputfn, chunk_size(), signaturefn, ignorepaths)
            with self.signing_ctx.signer(oidc_token) as signer:
                manifest = Manifest(serialized_paths)
                result = signer.sign(input_=manifest.to_intoto_statement())
                with signaturefn.open(mode="w") as b:
                    print(result.to_json(), file=b)
            return SignatureResult()
        except ExpiredIdentity:
            return SignatureResult(success=False,
                                   reason="exception caught: Signature failed: identity token has expired")  # noqa: E501
        except ExpiredCertificate:
            return SignatureResult(success=False,
                                   reason="exception caught: Signature failed: Fulcio signing certificate has expired")  # noqa: E501
        except Exception as e:
            return SignatureResult(success=False,
                                   reason=f"exception caught: {str(e)}")


# TODO: re-visit error handling and use a verbosity mode
# to avoid leaking info
class VerificationResult(BaseResult):
    pass


class SigstoreVerifier():
    def __init__(self, oidc_provider: str, identity: str):
        self.oidc_provider = oidc_provider
        self.identity = identity
        self.verifier = Verifier.production()

    # NOTE: Only path in the top-level folder are considered for ignorepaths.
    def verify(self, inputfn: Path, signaturefn: Path,
               ignorepaths: [Path], offline: bool) -> VerificationResult:
        try:
            bundle_bytes = signaturefn.read_bytes()
            bundle = Bundle().from_json(bundle_bytes)

            material: tuple[Path, VerificationMaterials]
            # TODO: verification
            # serialized_paths = Serializer.serialize_v2(
            #     inputfn, chunk_size(), signaturefn, ignorepaths)
            #     manifest = Manifest(serialized_paths)
            #     result = signer.sign(input_=manifest.to_intoto_statement())
            contentio = io.BytesIO(Serializer.serialize_v1(
                inputfn, chunk_size(), signaturefn, ignorepaths))
            material = VerificationMaterials.from_bundle(input_=contentio,
                                                         bundle=bundle,
                                                         offline=offline)
            policy_ = policy.Identity(
                identity=self.identity,
                issuer=self.oidc_provider,
            )
            result = self.verifier.verify(materials=material, policy=policy_)
            if result:
                return VerificationResult()
            return VerificationResult(success=False, reason=result.reason)
        except Exception as e:
            return VerificationResult(success=False,
                                      reason=f"exception caught: {str(e)}")
        raise ValueError("unreachable")
