import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'features/face_analysis/presentation/face_analysis_screen.dart';
import 'features/liveness/presentation/liveness_screen.dart';

void main() {
  runApp(const ProviderScope(child: RekognitionApp()));
}

class RekognitionApp extends StatelessWidget {
  const RekognitionApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Rekognition Demo',
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFFFF9900),
        useMaterial3: true,
      ),
      home: const HomeScreen(),
    );
  }
}

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _currentIndex = 0;

  static const _screens = <Widget>[
    FaceAnalysisScreen(),
    LivenessScreen(),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: _screens[_currentIndex],
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: (index) => setState(() => _currentIndex = index),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.face_retouching_natural),
            label: 'Detect',
          ),
          NavigationDestination(
            icon: Icon(Icons.verified_user),
            label: 'Liveness',
          ),
        ],
      ),
    );
  }
}
