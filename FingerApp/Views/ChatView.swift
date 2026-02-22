import SwiftUI
import SwiftData
import PhotosUI

struct ChatView: View {
    @Bindable var conversation: Conversation
    @Environment(\.modelContext) private var modelContext
    @State private var client = AssistantClient()
    @State private var input = ""
    @State private var scrollTask: Task<Void, Never>?
    @State private var speech = SpeechManager()
    @FocusState private var isInputFocused: Bool

    private var sortedMessages: [Message] {
        conversation.sortedMessages
    }

    var body: some View {
        GlassEffectContainer {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            ForEach(sortedMessages) { msg in
                                MessageBubble(
                                    role: msg.role,
                                    text: msg.text,
                                    onChoiceSelected: { choice in
                                        sendMessage(text: choice)
                                    }
                                )
                                .id(msg.id)
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
                    .scrollDismissesKeyboard(.interactively)
                    .scrollEdgeEffectStyle(.soft, for: .top)
                    .onTapGesture { isInputFocused = false }
                    .onChange(of: client.currentResponse) {
                        scrollTask?.cancel()
                        scrollTask = Task {
                            try? await Task.sleep(for: .milliseconds(50))
                            guard !Task.isCancelled else { return }
                            proxy.scrollTo("streaming", anchor: .bottom)
                        }
                    }
                    .onChange(of: sortedMessages.count) {
                        if let lastID = sortedMessages.last?.id {
                            proxy.scrollTo(lastID, anchor: .bottom)
                        }
                    }
                }

                ChatInputBar(
                    text: $input,
                    isFocused: $isInputFocused,
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
            .background { AppTheme.wallpaper.resizable().aspectRatio(contentMode: .fill).ignoresSafeArea() }
        }
        .navigationTitle(conversation.title)
        .navigationBarTitleDisplayMode(.inline)
    }

    private func sendMessage(text override: String? = nil) {
        let text = (override ?? input).trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        input = ""

        let userMessage = Message(role: "user", text: text, conversation: conversation)
        withAnimation(.spring(duration: 0.3)) {
            modelContext.insert(userMessage)
            conversation.updatedAt = Date()
            conversation.generateTitle()
        }

        Task {
            await client.send(message: text, sessionID: conversation.sessionID)

            let assistantMessage = Message(
                role: "assistant",
                text: client.currentResponse,
                conversation: conversation
            )
            withAnimation(.spring(duration: 0.3)) {
                modelContext.insert(assistantMessage)
                conversation.updatedAt = Date()
            }
        }
    }
}

// MARK: - Message Bubble

private struct MessageBubble: View {
    let role: String
    let text: String
    var onChoiceSelected: ((String) -> Void)?
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

    /// Parse numbered choices from assistant text (e.g. "1. Option A\n2. Option B")
    private var parsedChoices: (preamble: String, choices: [String])? {
        guard !isUser else { return nil }
        let lines = text.components(separatedBy: "\n")
        var preamble: [String] = []
        var choices: [String] = []
        var foundChoices = false

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.range(of: #"^\d+[\.\)]\s+"#, options: .regularExpression) != nil {
                foundChoices = true
                let choiceText = trimmed.replacingOccurrences(
                    of: #"^\d+[\.\)]\s+"#, with: "", options: .regularExpression
                )
                choices.append(choiceText)
            } else if !foundChoices {
                preamble.append(line)
            }
        }

        return choices.count >= 2 ? (preamble.joined(separator: "\n"), choices) : nil
    }

    /// Render assistant text as markdown AttributedString
    private var markdownString: AttributedString {
        var result = (try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(text)
        result.foregroundColor = .white
        return result
    }

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 60) }

            VStack(alignment: .leading, spacing: 8) {
                if let parsed = parsedChoices {
                    if !parsed.preamble.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text(parsed.preamble)
                            .font(.subheadline)
                            .foregroundColor(.white)
                    }
                    ChoiceButtonsView(choices: parsed.choices) { choice in
                        onChoiceSelected?(choice)
                    }
                } else if isUser {
                    Text(text)
                        .font(.subheadline)
                        .foregroundColor(Color(hex: "111827"))
                } else {
                    Text(markdownString)
                        .font(.subheadline)
                }
            }
            .padding(12)
            .background {
                if isUser {
                    Color.white.clipShape(userShape)
                }
            }
            .if(!isUser) { view in
                view.glassEffect(in: assistantShape)
            }

            if !isUser { Spacer(minLength: 60) }
        }
    }
}

// MARK: - Chat Input Bar

struct ChatInputBar: View {
    @Binding var text: String
    var isFocused: FocusState<Bool>.Binding
    let isProcessing: Bool
    let isRecording: Bool
    let transcript: String
    let onSend: () -> Void
    let onStartRecording: () -> Void
    let onStopRecording: () -> Void

    @State private var selectedPhoto: PhotosPickerItem?
    @State private var showComingSoon = false

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isProcessing
    }

    var body: some View {
        HStack(spacing: 12) {
            // Plus button for image upload
            PhotosPicker(selection: $selectedPhoto, matching: .images) {
                Image(systemName: "plus")
                    .font(.system(size: 18, weight: .medium))
                    .foregroundColor(.white)
                    .frame(width: 36, height: 36)
                    .background(Color.black.opacity(0.2))
                    .clipShape(Circle())
                    .overlay(Circle().stroke(AppTheme.border, lineWidth: 1))
            }
            .onChange(of: selectedPhoto) {
                showComingSoon = true
                selectedPhoto = nil
            }

            if isRecording {
                HStack(spacing: 10) {
                    Image(systemName: "mic.fill")
                        .font(.title3)
                        .foregroundColor(AppTheme.accent)
                        .symbolEffect(.pulse)
                    Text(transcript.isEmpty ? "Listening..." : transcript)
                        .font(.subheadline)
                        .foregroundColor(transcript.isEmpty ? AppTheme.textTertiary : .white)
                        .lineLimit(2)
                        .truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .glassEffect(.regular.tint(.blue), in: RoundedRectangle(cornerRadius: 20))
            } else {
                TextField("Type a message...", text: $text, axis: .vertical)
                    .lineLimit(1...5)
                    .focused(isFocused)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    .scrollContentBackground(.hidden)
                    .background(Color.black.opacity(0.2))
                    .foregroundColor(.white)
                    .clipShape(RoundedRectangle(cornerRadius: 20))
                    .overlay(
                        RoundedRectangle(cornerRadius: 20)
                            .stroke(
                                isFocused.wrappedValue ? AppTheme.accent.opacity(0.5) : AppTheme.border,
                                lineWidth: 1
                            )
                    )
            }

            if canSend {
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
                    .background(isRecording ? Color.red.opacity(0.8) : Color.black.opacity(0.2))
                    .clipShape(Circle())
                    .overlay {
                        if !isRecording {
                            Circle().stroke(AppTheme.border, lineWidth: 1)
                        }
                    }
                    .gesture(
                        LongPressGesture(minimumDuration: 0.3)
                            .sequenced(before: DragGesture(minimumDistance: 0))
                            .onChanged { value in
                                switch value {
                                case .second(true, _):
                                    if !isRecording { onStartRecording() }
                                default:
                                    break
                                }
                            }
                            .onEnded { _ in
                                onStopRecording()
                            }
                    )
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .glassEffect()
        .animation(.spring(duration: 0.2), value: isRecording)
        .alert("Coming Soon", isPresented: $showComingSoon) {
            Button("OK") {}
        } message: {
            Text("Image sharing will be available in a future update when vision model support is added.")
        }
    }
}

// MARK: - View Extension

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
