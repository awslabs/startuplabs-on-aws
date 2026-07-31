import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cognito from 'aws-cdk-lib/aws-cognito';

export interface RekognitionBackendStackProps extends cdk.StackProps {
  /** Origin allowed for CORS. Defaults to the Next.js dev server. */
  readonly allowedOrigin?: string;
}

/**
 * Backend for Amazon Rekognition Face Detection + Face Liveness.
 *
 * Detection flow:
 *   API Gateway (REST + API key + throttling) -> Lambda -> Rekognition DetectFaces/DetectLabels
 *
 * Liveness flow:
 *   1. App calls POST /liveness/create-session -> Lambda -> CreateFaceLivenessSession
 *   2. App streams video directly to Rekognition using temp creds from Cognito Identity Pool
 *   3. App calls GET /liveness/session/{sessionId}/result -> Lambda -> GetFaceLivenessSessionResults
 *
 * The mobile app never holds long-lived AWS credentials.
 */
export class RekognitionBackendStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: RekognitionBackendStackProps) {
    super(scope, id, props);

    const allowedOrigin = props?.allowedOrigin ?? 'http://localhost:3000';

    // ====================================================================
    // Detection Lambda (existing)
    // ====================================================================
    const detectFn = new lambda.Function(this, 'DetectFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('lambda/detect'),
      memorySize: 512,
      timeout: cdk.Duration.seconds(15),
      logGroup: new logs.LogGroup(this, 'DetectFunctionLogs', {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      description: 'Rekognition proxy — detect faces/labels',
    });

    detectFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['rekognition:DetectFaces', 'rekognition:DetectLabels'],
        resources: ['*'],
      }),
    );

    // ====================================================================
    // Liveness Lambda
    // ====================================================================
    const livenessFn = new lambda.Function(this, 'LivenessFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('lambda/liveness'),
      memorySize: 512,
      timeout: cdk.Duration.seconds(15),
      logGroup: new logs.LogGroup(this, 'LivenessFunctionLogs', {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      description: 'Rekognition proxy — Face Liveness session management',
    });

    livenessFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'rekognition:CreateFaceLivenessSession',
          'rekognition:GetFaceLivenessSessionResults',
        ],
        resources: ['*'],
      }),
    );

    // ====================================================================
    // Cognito Identity Pool (unauthenticated access for liveness streaming)
    // ====================================================================
    const identityPool = new cognito.CfnIdentityPool(this, 'LivenessIdentityPool', {
      identityPoolName: 'rekognition-liveness-pool',
      allowUnauthenticatedIdentities: true,
    });

    const unauthRole = new iam.Role(this, 'LivenessUnauthRole', {
      assumedBy: new iam.FederatedPrincipal(
        'cognito-identity.amazonaws.com',
        {
          'StringEquals': {
            'cognito-identity.amazonaws.com:aud': identityPool.ref,
          },
          'ForAnyValue:StringLike': {
            'cognito-identity.amazonaws.com:amr': 'unauthenticated',
          },
        },
        'sts:AssumeRoleWithWebIdentity',
      ),
      description: 'Least-privilege role for Face Liveness streaming from mobile app',
    });

    unauthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['rekognition:StartFaceLivenessSession'],
        resources: ['*'],
      }),
    );

    new cognito.CfnIdentityPoolRoleAttachment(this, 'LivenessIdentityPoolRoles', {
      identityPoolId: identityPool.ref,
      roles: {
        unauthenticated: unauthRole.roleArn,
      },
    });

    // ====================================================================
    // API Gateway
    // ====================================================================
    const api = new apigw.RestApi(this, 'RekognitionApi', {
      restApiName: 'rekognition-proxy',
      description: 'Proxy for Amazon Rekognition (detect + liveness)',
      deployOptions: {
        stageName: 'prod',
        throttlingRateLimit: 10,
        throttlingBurstLimit: 20,
        loggingLevel: apigw.MethodLoggingLevel.INFO,
        metricsEnabled: true,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: [allowedOrigin],
        allowMethods: ['POST', 'GET', 'OPTIONS'],
        allowHeaders: ['content-type', 'x-api-key'],
      },
    });

    const detectIntegration = new apigw.LambdaIntegration(detectFn);
    const livenessIntegration = new apigw.LambdaIntegration(livenessFn);
    const apiKeyRequired = true;

    // Detection routes
    for (const path of ['detect-faces', 'detect-labels']) {
      api.root
        .addResource(path)
        .addMethod('POST', detectIntegration, { apiKeyRequired });
    }

    // Liveness routes
    const livenessResource = api.root.addResource('liveness');

    livenessResource
      .addResource('create-session')
      .addMethod('POST', livenessIntegration, { apiKeyRequired });

    const sessionResource = livenessResource
      .addResource('session')
      .addResource('{sessionId}');

    sessionResource
      .addResource('result')
      .addMethod('GET', livenessIntegration, { apiKeyRequired });

    // API key + usage plan
    const apiKey = api.addApiKey('RekognitionApiKey');
    const plan = api.addUsagePlan('RekognitionUsagePlan', {
      throttle: { rateLimit: 10, burstLimit: 20 },
      quota: { limit: 10_000, period: apigw.Period.DAY },
    });
    plan.addApiKey(apiKey);
    plan.addApiStage({ stage: api.deploymentStage });

    // ====================================================================
    // Outputs
    // ====================================================================
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: api.url,
      description: 'Base URL for --dart-define=REKOGNITION_API_URL',
    });
    new cdk.CfnOutput(this, 'ApiKeyId', {
      value: apiKey.keyId,
      description: 'Retrieve value: aws apigateway get-api-key --api-key <id> --include-value',
    });
    new cdk.CfnOutput(this, 'IdentityPoolId', {
      value: identityPool.ref,
      description: 'Cognito Identity Pool ID for liveness streaming credentials',
    });
    new cdk.CfnOutput(this, 'LivenessUnauthRoleArn', {
      value: unauthRole.roleArn,
      description: 'IAM role used by unauthenticated liveness sessions',
    });
  }
}
