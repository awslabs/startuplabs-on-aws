/// Runtime configuration, injected at build time via --dart-define.
///
/// Never hardcode secrets in source. Pass them at build:
///
///   flutter run \
///     --dart-define=REKOGNITION_API_URL=https://<id>.execute-api.us-east-1.amazonaws.com/prod/ \
///     --dart-define=REKOGNITION_API_KEY=<key> \
///     --dart-define=IDENTITY_POOL_ID=<region>:<uuid> \
///     --dart-define=AWS_REGION=us-east-1
class AppConfig {
  const AppConfig._();

  static const apiUrl = String.fromEnvironment('REKOGNITION_API_URL');
  static const apiKey = String.fromEnvironment('REKOGNITION_API_KEY');
  static const identityPoolId = String.fromEnvironment('IDENTITY_POOL_ID');
  static const region =
      String.fromEnvironment('AWS_REGION', defaultValue: 'us-east-1');

  // Uri.resolve drops the last path segment when the base has no trailing
  // slash ('/prod' + 'liveness/x' => '/liveness/x'), so normalize it here.
  static Uri get baseUrl =>
      Uri.parse(apiUrl.endsWith('/') ? apiUrl : '$apiUrl/');
}
