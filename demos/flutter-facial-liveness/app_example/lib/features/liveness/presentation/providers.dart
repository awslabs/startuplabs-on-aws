import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/cognito_credentials_provider.dart';
import '../data/liveness_repository_impl.dart';
import '../domain/liveness_repository.dart';
import '../../../core/config.dart';

final livenessRepositoryProvider = Provider<LivenessRepository>((ref) {
  return LivenessRepositoryImpl();
});

final cognitoCredentialsProvider =
    Provider<CognitoCredentialsProvider>((ref) {
  return CognitoCredentialsProvider(
    identityPoolId: AppConfig.identityPoolId,
    region: AppConfig.region,
  );
});
