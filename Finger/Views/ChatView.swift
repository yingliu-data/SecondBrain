import SwiftUI

struct ChatView: View {
    @State private var client = AssistantClient()
    @State private var input = ""
    @State private var messages: [(role: String, text: String)] = []
    @State private var scrollTask: Task<Void, Never>?
    @State private var speech = SpeechManager()

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Finger")
                    .font(.title2.weight(.semibold))
                    .foregroundColor(.white)
                Text("Always here to help")
                    .font(.subheadline)
                    .foregroundColor(AppTheme.textTertiary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))
            .padding(.horizontal, 16)
            .padding(.top, 8)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(Array(messages.enumerated()), id: \.offset) { i, msg in
                            MessageBubble(role: msg.role, text: msg.text)
                                .id(i)
                                .transition(.asymmetric(
                                    insertion: .opacity.combined(with: .move(edge: .bottom)),
                                    removal: .opacity
                                ))
                        }
                        if client.isProcessing && !client.currentResponse.isEmpty {
                            MessageBubble(role: "assistant", text: client.currentResponse)
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

            ChatInputBar(
                text: $input,
                isProcessing: client.isProcessing,
                isRecording: speech.isRecording,
                transcript: speech.transcript,
                onSend: { sendMessage() },
                onStartRecording: { speech.startRecording() },
                onStopRecording: {
                    let text = speech.transcript
                    speech.stopRecording()
                    sendMessage(text: text)
                }
            )
        }
    }

    private func sendMessage(text override: String? = nil) {
        let text = (override ?? input).trimmingCharacters(in: .whitespaces)
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

private struct MessageBubble: View {
    let role: String
    let text: String
    private var isUser: Bool { role == "user" }

    private var assistantShape: UnevenRoundedRectangle {
        UnevenRoundedRectangle(
            topLeadingRadius: 8,
            bottomLeadingRadius: AppTheme.radiusLg,
            bottomTrailingRadius: AppTheme.radiusLg,
            topTrailingRadius: AppTheme.radiusLg
        )
    }

    private var userShape: UnevenRoundedRectangle {
        UnevenRoundedRectangle(
            topLeadingRadius: AppTheme.radiusLg,
            bottomLeadingRadius: AppTheme.radiusLg,
            bottomTrailingRadius: AppTheme.radiusLg,
            topTrailingRadius: 8
        )
    }

    var body: some View {
        GeometryReader { geo in
            let minSpacer = geo.size.width * 0.2
            HStack {
                if isUser { Spacer(minLength: minSpacer) }

                Text(text)
                    .font(.subheadline)
                    .padding(12)
                    .foregroundColor(isUser ? Color(hex: "111827") : .white)
                    .background {
                        if isUser {
                            Color.white.clipShape(userShape)
                        }
                    }
                    .if(!isUser) { view in
                        view.glassEffect(in: assistantShape)
                    }

                if !isUser { Spacer(minLength: minSpacer) }
            }
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

struct ChatInputBar: View {
    @Binding var text: String
    let isProcessing: Bool
    let isRecording: Bool
    let transcript: String
    let onSend: () -> Void
    let onStartRecording: () -> Void
    let onStopRecording: () -> Void
    @FocusState private var isFocused: Bool

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespaces).isEmpty && !isProcessing
    }

    var body: some View {
        HStack(spacing: 12) {
            if isRecording {
                HStack(spacing: 10) {
                    Image(systemName: "mic.fill")
                        .font(.title3)
                        .foregroundColor(AppTheme.emerald)
                        .symbolEffect(.pulse)
                    Text(transcript.isEmpty ? "Listening..." : transcript)
                        .font(.subheadline)
                        .foregroundColor(transcript.isEmpty ? AppTheme.textTertiary : .white)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .glassEffect(.regular.tint(.green), in: RoundedRectangle(cornerRadius: 20))
            } else {
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
            }

            if isRecording {
                Button(action: onStopRecording) {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundColor(.white)
                        .frame(width: 40, height: 40)
                        .background(Color.red.opacity(0.8))
                        .clipShape(Circle())
                }
            } else if canSend {
                Button(action: onSend) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundColor(.white)
                        .frame(width: 40, height: 40)
                        .background(AppTheme.accentGradient)
                        .clipShape(Circle())
                }
            } else {
                Image(systemName: "mic.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundColor(.white)
                    .frame(width: 40, height: 40)
                    .background(Color.black.opacity(0.2))
                    .clipShape(Circle())
                    .overlay(Circle().stroke(AppTheme.border, lineWidth: 1))
                    .onLongPressGesture(minimumDuration: 0.3) {
                        onStartRecording()
                    }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .glassEffect()
        .animation(.spring(duration: 0.2), value: isRecording)
    }
}

private extension View {
    @ViewBuilder
    func `if`<Transform: View>(_ condition: Bool, transform: (Self) -> Transform) -> some View {
        if condition {
            transform(self)
        } else {
            self
        }
    }
}
