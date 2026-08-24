import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'package:rekognition_app_example/main.dart';

void main() {
  testWidgets('App renders navigation bar', (WidgetTester tester) async {
    await tester.pumpWidget(const ProviderScope(child: RekognitionApp()));
    expect(find.text('Detect'), findsOneWidget);
    expect(find.text('Liveness'), findsOneWidget);
  });
}
