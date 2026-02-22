# Changelog

## 0.8.0 - 22 February 2026

### iOS App
- fix: keyboard dismissal with interactive scroll and tap gesture
- fix: multiline text input with auto-growing TextField(axis: .vertical)
- feat: scroll fade effect at top using iOS 26 scrollEdgeEffectStyle
- feat: multi-conversation support with SwiftData persistence
- feat: conversation list with create/switch between chats
- feat: NavigationStack with Liquid Glass back button
- feat: markdown rendering in assistant message bubbles
- feat: interactive choice buttons for LLM option responses
- feat: image upload "+" button placeholder (Coming Soon)
- feat: device vs server skill badges in Skills view

### Server
- fix: system prompt enforces shorter, plain-text responses
- fix: reduce max response tokens from 1024 to 512

## 0.7.1 - 22 February 2026

- Finger: Fix push-to-talk mic gesture (view identity preservation)
- Finger: Add debug log viewer in Settings → Developer
- Finger: Backend request/response logging via DebugLog

## 0.7.0 - 22 February 2026

- Finger: iOS 26 Liquid Glass effect on all surfaces (native .glassEffect API)
- Finger: Native TabView with auto Liquid Glass tab bar
- Finger: Add New Skill chatbot connected to real LLM
- Finger: Hardcoded credentials, removed server config from Settings UI
- Finger: Code cleanup across all views

## 0.6.0 - 22 February 2026

- Finger: Glass effect on all surfaces, Add New Skill chat flow, configurable server URL and API key
- Finger: Voice input via long-press mic button with on-device speech recognition

## 0.5.0 - 22 February 2026

- Finger: Dark-first UI with glass-morphism, emerald accents, tab bar, Skills and Settings views

## 0.4.1 - 22 February 2026

- Finger: Add required Info.plist privacy descriptions for Calendar, Reminders, and Contacts

## 0.4.0 - 21 February 2026

- Finger: On-device tool executors for Calendar, Reminders, Contacts, and Clipboard

## 0.3.0 - 21 February 2026

- Finger iOS client: SwiftUI chat app with HMAC auth, SSE streaming, and Cloudflare Access headers

## 0.2.0 - 21 February 2026

- Agent API: Modular architecture with skills system, native LLM function calling, and CRUD skills API

## 0.1.1 - 20 February 2026

- Agent API: Full agent loop with auth, tool calls, SSE streaming, and prompt injection defense

## 0.1.0 - 20 February 2026

- Infrastructure: Add GitHub Actions CI/CD deploy workflow, docker-compose, and agent-api stub
