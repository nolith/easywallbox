# paho-mqtt_1 from nixos-24.11
{
  lib,
  stdenv,
  buildPythonPackage,
  fetchFromGitHub,
  isPy3k,
  pytestCheckHook,
  mock,
  six,
}:

buildPythonPackage rec {
  pname = "paho-mqtt";
  version = "1.6.1";
  format = "setuptools";

  src = fetchFromGitHub {
    owner = "eclipse";
    repo = "paho.mqtt.python";
    rev = "v${version}";
    hash = "sha256-9nH6xROVpmI+iTKXfwv2Ar1PAmWbEunI3HO0pZyK6Rg=";
  };

  nativeCheckInputs = [
    pytestCheckHook
    six
  ] ++ lib.optionals (!isPy3k) [ mock ];

  doCheck = !stdenv.hostPlatform.isDarwin;

  pythonImportsCheck = [ "paho.mqtt" ];

  meta = with lib; {
    description = "MQTT version 3.1.1 client class";
    homepage = "https://eclipse.org/paho";
    license = licenses.epl10;
    maintainers = with maintainers; [
      mog
      dotlambda
    ];
  };
}
