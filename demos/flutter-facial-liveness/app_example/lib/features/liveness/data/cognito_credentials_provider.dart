import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:rekognition_liveness/rekognition_liveness.dart';

class CognitoCredentialsProvider implements LivenessCredentialsProvider {
  CognitoCredentialsProvider({
    required this.identityPoolId,
    required this.region,
    http.Client? client,
  }) : _client = client ?? http.Client();

  final String identityPoolId;
  final String region;
  final http.Client _client;

  @override
  Future<LivenessCredentials> fetchCredentials() async {
    final cognitoEndpoint =
        'https://cognito-identity.$region.amazonaws.com';

    final getIdResponse = await _client.post(
      Uri.parse(cognitoEndpoint),
      headers: {
        'content-type': 'application/x-amz-json-1.1',
        'x-amz-target': 'AWSCognitoIdentityService.GetId',
      },
      body: jsonEncode({'IdentityPoolId': identityPoolId}),
    );

    if (getIdResponse.statusCode != 200) {
      throw Exception('GetId failed: ${getIdResponse.body}');
    }

    final identityId =
        (jsonDecode(getIdResponse.body) as Map<String, dynamic>)['IdentityId']
            as String;

    final getCredsResponse = await _client.post(
      Uri.parse(cognitoEndpoint),
      headers: {
        'content-type': 'application/x-amz-json-1.1',
        'x-amz-target':
            'AWSCognitoIdentityService.GetCredentialsForIdentity',
      },
      body: jsonEncode({'IdentityId': identityId}),
    );

    if (getCredsResponse.statusCode != 200) {
      throw Exception(
          'GetCredentialsForIdentity failed: ${getCredsResponse.body}');
    }

    final creds = (jsonDecode(getCredsResponse.body)
        as Map<String, dynamic>)['Credentials'] as Map<String, dynamic>;

    return LivenessCredentials(
      accessKeyId: creds['AccessKeyId'] as String,
      secretAccessKey: creds['SecretKey'] as String,
      sessionToken: creds['SessionToken'] as String,
    );
  }
}
