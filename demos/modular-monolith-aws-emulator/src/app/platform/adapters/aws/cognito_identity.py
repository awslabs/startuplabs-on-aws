"""Cognito user pool adapter (IdentityProvider port)."""

from __future__ import annotations

from typing import Any

from app.platform.adapters.aws.errors import translate
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.ports.identity import SignUpResult, Tokens, UserInfo
from app.shared import errors

_MAPPING = {
    "UsernameExistsException": errors.Conflict,
    "InvalidPasswordException": errors.ValidationFailed,
    "InvalidParameterException": errors.ValidationFailed,
    "CodeMismatchException": errors.ValidationFailed,
    "ExpiredCodeException": errors.ValidationFailed,
    "NotAuthorizedException": lambda _m: errors.Unauthorized("invalid credentials"),
    "UserNotFoundException": lambda _m: errors.Unauthorized("invalid credentials"),
    "UserNotConfirmedException": lambda _m: errors.Forbidden("user not confirmed"),
    "TooManyRequestsException": errors.PlatformUnavailable,
}


class CognitoIdentityProvider:
    def __init__(
        self,
        clients: AwsClientFactory,
        user_pool_id: str,
        client_id: str,
        *,
        auto_confirm: bool = False,
    ) -> None:
        self._idp = clients.client("cognito-idp")
        self._pool = user_pool_id
        self._client_id = client_id
        self._auto_confirm = auto_confirm

    def sign_up(self, email: str, password: str, name: str) -> SignUpResult:
        with translate(_MAPPING):
            resp = self._idp.sign_up(
                ClientId=self._client_id,
                Username=email,
                Password=password,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "name", "Value": name},
                ],
            )
            confirmed = bool(resp["UserConfirmed"])
            if self._auto_confirm and not confirmed:
                self._idp.admin_confirm_sign_up(UserPoolId=self._pool, Username=email)
                confirmed = True
        return SignUpResult(subject=resp["UserSub"], confirmed=confirmed)

    def confirm(self, email: str, code: str) -> None:
        with translate(_MAPPING):
            self._idp.confirm_sign_up(
                ClientId=self._client_id, Username=email, ConfirmationCode=code
            )

    def authenticate(self, email: str, password: str) -> Tokens:
        with translate(_MAPPING):
            resp = self._idp.initiate_auth(
                ClientId=self._client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
        return self._tokens(resp, refresh_token=None)

    def refresh(self, refresh_token: str) -> Tokens:
        with translate(_MAPPING):
            resp = self._idp.initiate_auth(
                ClientId=self._client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={"REFRESH_TOKEN": refresh_token},
            )
        return self._tokens(resp, refresh_token=refresh_token)

    def sign_out(self, access_token: str) -> None:
        with translate(_MAPPING):
            self._idp.global_sign_out(AccessToken=access_token)

    def add_to_group(self, email: str, group: str) -> None:
        with translate(_MAPPING):
            self._idp.admin_add_user_to_group(
                UserPoolId=self._pool, Username=email, GroupName=group
            )

    def user_info(self, access_token: str) -> UserInfo:
        with translate(_MAPPING):
            resp = self._idp.get_user(AccessToken=access_token)
        attrs = {a["Name"]: a["Value"] for a in resp["UserAttributes"]}
        return UserInfo(
            subject=attrs.get("sub", resp["Username"]),
            email=attrs.get("email", ""),
            name=attrs.get("name", attrs.get("email", "")),
        )

    @staticmethod
    def _tokens(resp: dict[str, Any], refresh_token: str | None) -> Tokens:
        if "AuthenticationResult" not in resp:
            # MFA / NEW_PASSWORD_REQUIRED challenges are not wired in the boilerplate.
            raise errors.Forbidden(f"challenge required: {resp.get('ChallengeName')}")
        result = resp["AuthenticationResult"]
        return Tokens(
            access_token=result["AccessToken"],
            id_token=result["IdToken"],
            refresh_token=result.get("RefreshToken", refresh_token),
            expires_in=int(result["ExpiresIn"]),
        )
