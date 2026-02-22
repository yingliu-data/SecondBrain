import SwiftUI

struct ChatView: View {
    @State private var client = AssistantClient()
    @State private var input = ""
    @State private var messages: [(role: String, text: String)] = []
    @State private var scrollTask: Task<Void, Never>?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            ForEach(Array(messages.enumerated()), id: \.offset) { i, msg in
                                MessageBubble(role: msg.role, text: msg.text)
                                    .id(i)
                            }
                            if client.isProcessing && !client.currentResponse.isEmpty {
                                MessageBubble(role: "assistant", text: client.currentResponse)
                                    .id("streaming")
                            }
                        }
                        .padding()
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

                Divider()

                HStack(spacing: 12) {
                    TextField("Ask me anything...", text: $input)
                        .textFieldStyle(.roundedBorder)
                        .submitLabel(.send)
                        .onSubmit { sendMessage() }

                    Button(action: sendMessage) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.title2)
                    }
                    .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty || client.isProcessing)
                }
                .padding()
            }
            .navigationTitle("Finger")
        }
    }

    private func sendMessage() {
        let text = input.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        input = ""
        messages.append((role: "user", text: text))

        Task {
            await client.send(message: text)
            messages.append((role: "assistant", text: client.currentResponse))
        }
    }
}

struct MessageBubble: View {
    let role: String
    let text: String

    var body: some View {
        HStack {
            if role == "user" { Spacer() }
            Text(text)
                .padding(12)
                .background(role == "user" ? Color.blue : Color(.systemGray5))
                .foregroundColor(role == "user" ? .white : .primary)
                .clipShape(RoundedRectangle(cornerRadius: 16))
            if role == "assistant" { Spacer() }
        }
    }
}

#Preview {
    ChatView()
}
