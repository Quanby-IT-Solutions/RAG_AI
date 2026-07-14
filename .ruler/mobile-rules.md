---
description: Flutter mobile app conventions for apps/mobile
globs: ["apps/mobile/**/*.dart"]
alwaysApply: false
---

# Flutter Mobile Rules (`apps/mobile/`)

## Directory Organization

```
apps/mobile/lib/
├── main.dart              # App entry point
├── app.dart               # MaterialApp/CupertinoApp configuration
├── core/                  # Shared utilities and base classes
│   ├── constants/         # App-wide constants
│   ├── extensions/        # Dart extension methods
│   ├── theme/             # Theme data and styling
│   └── utils/             # Utility functions
├── features/              # Feature-based organization
│   └── [feature]/
│       ├── data/          # Data layer (repositories, data sources)
│       │   ├── models/    # Data models, DTOs
│       │   └── repositories/
│       ├── domain/        # Business logic (optional clean architecture)
│       └── presentation/  # UI layer
│           ├── screens/   # Full-page widgets
│           ├── widgets/   # Feature-specific widgets
│           └── providers/ # State management (Riverpod/Provider)
├── services/              # External service integrations
│   ├── api/               # HTTP client, API service
│   ├── auth/              # Authentication service
│   └── storage/           # Local storage service
└── shared/                # Shared widgets and components
    ├── widgets/           # Reusable UI components
    └── dialogs/           # Common dialogs
```

## File Naming Conventions

Use **snake_case** for all file names (required by Dart linter):

| Type         | Pattern                               | Example                                     |
| ------------ | ------------------------------------- | ------------------------------------------- |
| Screens      | `[name]_screen.dart`                  | `home_screen.dart`, `login_screen.dart`     |
| Widgets      | `[name]_widget.dart` or `[name].dart` | `user_avatar.dart`, `todo_card.dart`        |
| Services     | `[name]_service.dart`                 | `auth_service.dart`, `api_service.dart`     |
| Models       | `[name]_model.dart` or `[name].dart`  | `user_model.dart`, `todo.dart`              |
| Providers    | `[name]_provider.dart`                | `auth_provider.dart`, `todos_provider.dart` |
| Repositories | `[name]_repository.dart`              | `user_repository.dart`                      |
| Extensions   | `[type]_extensions.dart`              | `string_extensions.dart`                    |

## Class Naming Conventions

Dart uses **PascalCase** for class names:

```dart
// Screens
class HomeScreen extends StatelessWidget {}
class LoginScreen extends StatefulWidget {}

// Widgets
class UserAvatar extends StatelessWidget {}
class TodoCard extends StatelessWidget {}

// Services
class AuthService {}
class ApiService {}

// Models
class User {}
class Todo {}

// Providers (Riverpod)
final todosProvider = StateNotifierProvider<TodosNotifier, List<Todo>>((ref) {
  return TodosNotifier();
});
```

## State Management

Recommended: **Riverpod** for state management

```dart
// providers/todos_provider.dart
final todosProvider = FutureProvider<List<Todo>>((ref) async {
  final apiService = ref.watch(apiServiceProvider);
  return apiService.getTodos();
});

// screens/todos_screen.dart
class TodosScreen extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final todosAsync = ref.watch(todosProvider);
    return todosAsync.when(
      data: (todos) => ListView.builder(...),
      loading: () => CircularProgressIndicator(),
      error: (err, stack) => Text('Error: $err'),
    );
  }
}
```

## Backend API Integration

### API Service Pattern

```dart
// services/api/api_service.dart
class ApiService {
  final Dio _dio;

  ApiService() : _dio = Dio(BaseOptions(
    baseUrl: Environment.apiBaseUrl,
  ));

  Future<List<Todo>> getTodos() async {
    final response = await _dio.get('/api/v1/examples/todos');
    return (response.data['data'] as List)
        .map((json) => Todo.fromJson(json))
        .toList();
  }
}
```

### Authentication

```dart
// services/auth/auth_service.dart
class AuthService {
  final FlutterSecureStorage _storage;

  Future<void> signIn(String email, String password) async {
    // Call backend auth endpoint
    final response = await _dio.post('/api/auth/sign-in', data: {
      'email': email,
      'password': password,
    });
    await _storage.write(key: 'token', value: response.data['token']);
  }
}
```

## Feature Module Pattern

Each feature should be self-contained:

```
features/todos/
├── data/
│   ├── models/
│   │   └── todo.dart
│   └── repositories/
│       └── todos_repository.dart
├── presentation/
│   ├── screens/
│   │   ├── todos_screen.dart
│   │   └── todo_detail_screen.dart
│   ├── widgets/
│   │   └── todo_card.dart
│   └── providers/
│       └── todos_provider.dart
```

## Best Practices

### Widget Composition

- Prefer composition over inheritance
- Extract widgets when they become complex (50+ lines)
- Use `const` constructors where possible

```dart
// Good: const constructor
class TodoCard extends StatelessWidget {
  const TodoCard({super.key, required this.todo});
  final Todo todo;
}
```

### Avoid Deep Nesting

```dart
// Bad: deeply nested
Scaffold(
  body: Container(
    child: Column(
      children: [
        Container(
          child: Row(
            children: [...]
          )
        )
      ]
    )
  )
)

// Good: extract widgets
Scaffold(
  body: TodoList(),
)
```

## Testing

- Widget tests: `test/` directory
- Integration tests: `integration_test/` directory
- Use `flutter_test` package

```dart
// test/features/todos/presentation/widgets/todo_card_test.dart
testWidgets('TodoCard displays title', (tester) async {
  await tester.pumpWidget(MaterialApp(
    home: TodoCard(todo: Todo(title: 'Test')),
  ));
  expect(find.text('Test'), findsOneWidget);
});
```
