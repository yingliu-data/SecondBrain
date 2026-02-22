import SwiftUI
import SwiftData

struct ConversationListView: View {
    @Environment(\.modelContext) private var modelContext
    @Query(sort: \Conversation.updatedAt, order: .reverse) private var conversations: [Conversation]
    @Binding var selectedConversation: Conversation?

    var body: some View {
        GlassEffectContainer {
            ScrollView {
                LazyVStack(spacing: 8) {
                    if conversations.isEmpty {
                        VStack(spacing: 12) {
                            Image(systemName: "message")
                                .font(.system(size: 40))
                                .foregroundColor(AppTheme.textTertiary)
                            Text("No conversations yet")
                                .font(.subheadline)
                                .foregroundColor(AppTheme.textTertiary)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.top, 80)
                    }

                    ForEach(conversations) { conv in
                        ConversationRow(conversation: conv)
                            .onTapGesture {
                                selectedConversation = conv
                            }
                    }
                }
                .padding(16)
            }
            .background { AppTheme.wallpaper.resizable().aspectRatio(contentMode: .fill).ignoresSafeArea() }
        }
        .navigationTitle("Finger")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: createNewChat) {
                    Image(systemName: "square.and.pencil")
                        .font(.title3)
                        .foregroundColor(AppTheme.accent)
                }
            }
        }
    }

    private func createNewChat() {
        let conversation = Conversation()
        modelContext.insert(conversation)
        selectedConversation = conversation
    }
}

private struct ConversationRow: View {
    let conversation: Conversation

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "message.fill")
                .font(.system(size: 16))
                .foregroundColor(AppTheme.accent)
                .frame(width: 36, height: 36)

            VStack(alignment: .leading, spacing: 4) {
                Text(conversation.title)
                    .font(.subheadline.weight(.medium))
                    .foregroundColor(.white)
                    .lineLimit(1)

                if let lastMessage = conversation.sortedMessages.last {
                    Text(lastMessage.text)
                        .font(.caption)
                        .foregroundColor(AppTheme.textTertiary)
                        .lineLimit(1)
                }
            }

            Spacer()

            Text(conversation.updatedAt.formatted(.relative(presentation: .named)))
                .font(.caption2)
                .foregroundColor(AppTheme.textTertiary)
        }
        .padding(12)
        .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusMd))
    }
}
