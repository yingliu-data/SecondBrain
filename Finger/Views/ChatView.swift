import SwiftUI

struct ChatView: View {
    @State private var client = AssistantClient()
    @State private var input = ""
    @State private var messages: [(role: String, text: String)] = []
    @State private var scrollTask: Task<Void, Never>?

    var body: some View {
        VStack(spacing: 0) {
            // Header card
            VStack(alignment: .leading, spacing: 4) {
                Text("Finger")
                    .font(.title2.weight(.semibold))
                    .foregroundColor(.white)
                Text("Always here to help")
                    .font(.subheadline)
                    .foregroundColor(AppTheme.textSubtle)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(AppTheme.glassBg)
            .background(.ultraThinMaterial)
            .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusLg))
            .overlay(
                RoundedRectangle(cornerRadius: AppTheme.radiusLg)
                    .stroke(AppTheme.border, lineWidth: 1)
            )
            .padding(.horizontal, 16)
            .padding(.top, 8)

            // Messages
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(Array(messages.enumerated()), id: \.offset) { i, msg in
                            StyledMessageBubble(role: msg.role, text: msg.text)
                                .id(i)
                                .transition(.asymmetric(
                                    insertion: .opacity.combined(with: .move(edge: .bottom)),
                                    removal: .opacity
                                ))
                        }
                        if client.isProcessing && !client.currentResponse.isEmpty {
                            StyledMessageBubble(role: "assistant", text: client.currentResponse)
                                .id("streaming")
                        }
                    }
                    .padding(16)
                }
                .onChange(of: client.currentResponse) {
                    scrollTask?.cancel()
                    scrollTask = Task {
                        try? await Task.sleep(for: .milliseconds(50))
                        guard !Task.isCancelled else { return }
                        proxy.scrollTo("streaming", anchor: .bottom)
                    }
                }
                .onChange(of: messages.count) {
                    proxy.scrollTo(messages.count - 1, anchor: .bottom)
                }
            }

            // Input bar
            ChatInputBar(text: $input, isProcessing: client.isProcessing) {
                sendMessage()
            }
        }
    }

    private func sendMessage() {
        let text = input.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        input = ""
        withAnimation(.spring(duration: 0.3)) {
            messages.append((role: "user", text: text))
        }

        Task {
            await client.send(message: text)
            withAnimation(.spring(duration: 0.3)) {
                messages.append((role: "assistant", text: client.currentResponse))
            }
        }
    }
}

// MARK: - Message Bubble

struct StyledMessageBubble: View {
    let role: String
    let text: String
    private var isUser: Bool { role == "user" }

    var body: some View {
        GeometryReader { geo in
            let minSpacer = geo.size.width * 0.2
            HStack {
                if isUser { Spacer(minLength: minSpacer) }

                Text(text)
                    .font(.subheadline)
                    .padding(12)
                    .background {
                        if isUser {
                            Color.white
                        } else {
                            AppTheme.glassBg
                                .background(.ultraThinMaterial)
                        }
                    }
                    .foregroundColor(isUser ? Color(hex: "111827") : .white)
                    .clipShape(
                        UnevenRoundedRectangle(
                            topLeadingRadius: isUser ? AppTheme.radiusLg : 8,
                            bottomLeadingRadius: AppTheme.radiusLg,
                            bottomTrailingRadius: AppTheme.radiusLg,
                            topTrailingRadius: isUser ? 8 : AppTheme.radiusLg
                        )
                    )
                    .overlay(
                        Group {
                            if !isUser {
                                UnevenRoundedRectangle(
                                    topLeadingRadius: 8,
                                    bottomLeadingRadius: AppTheme.radiusLg,
                                    bottomTrailingRadius: AppTheme.radiusLg,
                                    topTrailingRadius: AppTheme.radiusLg
                                )
                                .stroke(AppTheme.emerald.opacity(0.2), lineWidth: 1)
                            }
                        }
                    )

                if !isUser { Spacer(minLength: minSpacer) }
            }
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Input Bar

struct ChatInputBar: View {
    @Binding var text: String
    let isProcessing: Bool
    let onSend: () -> Void
    @FocusState private var isFocused: Bool

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespaces).isEmpty && !isProcessing
    }

    var body: some View {
        HStack(spacing: 12) {
            TextField("Type a message...", text: $text)
                .focused($isFocused)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(Color.black.opacity(0.2))
                .foregroundColor(.white)
                .clipShape(RoundedRectangle(cornerRadius: 20))
                .overlay(
                    RoundedRectangle(cornerRadius: 20)
                        .stroke(
                            isFocused ? AppTheme.emerald.opacity(0.5) : AppTheme.border,
                            lineWidth: 1
                        )
                )
                .submitLabel(.send)
                .onSubmit { if canSend { onSend() } }

            Button(action: onSend) {
                Image(systemName: "arrow.up")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundColor(.white)
                    .frame(width: 40, height: 40)
                    .background(AppTheme.accentGradient)
                    .clipShape(Circle())
                    .shadow(color: AppTheme.emerald.opacity(0.3), radius: 8, y: 2)
            }
            .disabled(!canSend)
            .opacity(canSend ? 1.0 : 0.5)
            .scaleEffect(canSend ? 1.0 : 0.9)
            .animation(.spring(duration: 0.2), value: canSend)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(AppTheme.glassBg)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            Rectangle()
                .fill(AppTheme.border)
                .frame(height: 0.5)
        }
    }
}

#Preview {
    ZStack {
        AppTheme.backgroundGradient.ignoresSafeArea()
        ChatView()
    }
}
