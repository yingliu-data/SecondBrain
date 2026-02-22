import SwiftUI

struct SkillItem: Identifiable {
    let id = UUID()
    let name: String
    let description: String
    let icon: String
    var enabled: Bool
}

struct SkillsView: View {
    @State private var skills: [SkillItem] = [
        SkillItem(name: "Calendar", description: "Check and create events", icon: "calendar", enabled: true),
        SkillItem(name: "Reminders", description: "Read and create reminders", icon: "checklist", enabled: true),
        SkillItem(name: "Contacts", description: "Search contacts by name", icon: "person.crop.circle", enabled: true),
        SkillItem(name: "Clipboard", description: "Read clipboard contents", icon: "doc.on.clipboard", enabled: true),
        SkillItem(name: "Web Search", description: "Search the web for info", icon: "globe", enabled: true),
    ]
    @State private var showingAddSkill = false

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                // Header
                GlassHeader(
                    icon: "bolt.fill",
                    iconColor: AppTheme.emerald,
                    title: "Skills",
                    subtitle: "Manage your assistant's capabilities"
                )

                // Add New Skill button
                Button {
                    showingAddSkill = true
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "plus")
                            .font(.title3.weight(.semibold))
                            .foregroundColor(.white)
                            .frame(width: 48, height: 48)
                            .background(AppTheme.accentGradient)
                            .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusMd))
                            .shadow(color: AppTheme.emerald.opacity(0.2), radius: 8, y: 2)

                        VStack(alignment: .leading, spacing: 2) {
                            Text("Add New Skill")
                                .font(.body.weight(.medium))
                                .foregroundColor(.white)
                            Text("Chat to configure a custom skill")
                                .font(.caption)
                                .foregroundColor(AppTheme.textSubtle)
                        }

                        Spacer()
                    }
                    .padding(16)
                    .background(AppTheme.glassBg)
                    .background(.ultraThinMaterial)
                    .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusLg))
                    .overlay(
                        RoundedRectangle(cornerRadius: AppTheme.radiusLg)
                            .stroke(AppTheme.border, lineWidth: 1)
                    )
                }
                .buttonStyle(.plain)

                // Skill cards
                ForEach(Array(skills.enumerated()), id: \.element.id) { index, _ in
                    SkillCard(skill: $skills[index])
                }
            }
            .padding(16)
        }
        .fullScreenCover(isPresented: $showingAddSkill) {
            AddSkillView()
        }
    }
}

// MARK: - Skill Card

private struct SkillCard: View {
    @Binding var skill: SkillItem

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: skill.icon)
                .font(.title3)
                .foregroundColor(AppTheme.emerald)
                .frame(width: 48, height: 48)
                .background(Color.black.opacity(0.2))
                .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusMd))
                .overlay(
                    RoundedRectangle(cornerRadius: AppTheme.radiusMd)
                        .stroke(AppTheme.border, lineWidth: 1)
                )

            VStack(alignment: .leading, spacing: 2) {
                Text(skill.name)
                    .font(.body.weight(.medium))
                    .foregroundColor(.white)
                Text(skill.description)
                    .font(.caption)
                    .foregroundColor(AppTheme.textSubtle)
            }

            Spacer()

            EmeraldToggle(isOn: $skill.enabled)
        }
        .padding(16)
        .background(AppTheme.glassBg)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusLg))
        .overlay(
            RoundedRectangle(cornerRadius: AppTheme.radiusLg)
                .stroke(AppTheme.border, lineWidth: 1)
        )
    }
}

// MARK: - Custom Toggle

struct EmeraldToggle: View {
    @Binding var isOn: Bool

    var body: some View {
        Button {
            withAnimation(.spring(duration: 0.25)) { isOn.toggle() }
        } label: {
            ZStack(alignment: isOn ? .trailing : .leading) {
                Capsule()
                    .fill(isOn ? AnyShapeStyle(AppTheme.accentGradient) : AnyShapeStyle(Color.white.opacity(0.1)))
                    .frame(width: 52, height: 32)
                Circle()
                    .fill(.white)
                    .shadow(color: .black.opacity(0.2), radius: 2)
                    .frame(width: 24, height: 24)
                    .padding(4)
            }
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Add Skill View

struct AddSkillView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var input = ""
    @State private var messages: [(id: UUID, text: String, isUser: Bool)] = [
        (id: UUID(), text: "Hi! I'll help you add a new skill. What would you like your AI assistant to be able to do?", isUser: false)
    ]
    @FocusState private var isFocused: Bool

    var body: some View {
        ZStack {
            AppTheme.backgroundGradient.ignoresSafeArea()

            VStack(spacing: 0) {
                // Header
                HStack(spacing: 12) {
                    Button {
                        dismiss()
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.body.weight(.semibold))
                            .foregroundColor(.white)
                            .frame(width: 36, height: 36)
                            .background(Color.black.opacity(0.2))
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                            .overlay(
                                RoundedRectangle(cornerRadius: 10)
                                    .stroke(AppTheme.border, lineWidth: 1)
                            )
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Image(systemName: "sparkles")
                                .foregroundColor(AppTheme.emerald)
                            Text("Add New Skill")
                                .font(.title3.weight(.semibold))
                                .foregroundColor(.white)
                        }
                        Text("Configure a custom capability")
                            .font(.caption)
                            .foregroundColor(AppTheme.textSubtle)
                    }

                    Spacer()
                }
                .padding(16)
                .background(AppTheme.glassBg)
                .background(.ultraThinMaterial)
                .overlay(alignment: .bottom) {
                    Rectangle().fill(AppTheme.border).frame(height: 0.5)
                }

                // Messages
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(messages, id: \.id) { msg in
                            HStack {
                                if msg.isUser { Spacer(minLength: 60) }

                                Text(msg.text)
                                    .font(.subheadline)
                                    .padding(12)
                                    .background {
                                        if msg.isUser {
                                            Color.white
                                        } else {
                                            AppTheme.glassBg
                                                .background(.ultraThinMaterial)
                                        }
                                    }
                                    .foregroundColor(msg.isUser ? Color(hex: "111827") : .white)
                                    .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusLg))
                                    .overlay(
                                        !msg.isUser
                                            ? RoundedRectangle(cornerRadius: AppTheme.radiusLg)
                                                .stroke(AppTheme.emerald.opacity(0.2), lineWidth: 1)
                                            : nil
                                    )

                                if !msg.isUser { Spacer(minLength: 60) }
                            }
                        }
                    }
                    .padding(16)
                }

                // Input
                HStack(spacing: 12) {
                    TextField("Describe your skill...", text: $input)
                        .focused($isFocused)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 12)
                        .background(Color.black.opacity(0.2))
                        .foregroundColor(.white)
                        .clipShape(RoundedRectangle(cornerRadius: 20))
                        .overlay(
                            RoundedRectangle(cornerRadius: 20)
                                .stroke(isFocused ? AppTheme.emerald.opacity(0.5) : AppTheme.border, lineWidth: 1)
                        )
                        .submitLabel(.send)
                        .onSubmit { sendMessage() }

                    Button(action: sendMessage) {
                        Image(systemName: "arrow.up")
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundColor(.white)
                            .frame(width: 40, height: 40)
                            .background(AppTheme.accentGradient)
                            .clipShape(Circle())
                            .shadow(color: AppTheme.emerald.opacity(0.3), radius: 8, y: 2)
                    }
                    .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty)
                    .opacity(input.trimmingCharacters(in: .whitespaces).isEmpty ? 0.5 : 1.0)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(AppTheme.glassBg)
                .background(.ultraThinMaterial)
                .overlay(alignment: .top) {
                    Rectangle().fill(AppTheme.border).frame(height: 0.5)
                }
            }
        }
        .preferredColorScheme(.dark)
    }

    private func sendMessage() {
        let text = input.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        input = ""
        withAnimation(.spring(duration: 0.3)) {
            messages.append((id: UUID(), text: text, isUser: true))
        }
        Task {
            try? await Task.sleep(for: .seconds(1))
            withAnimation(.spring(duration: 0.3)) {
                messages.append((id: UUID(), text: "Great! I'll configure that skill for you. Can you provide more details about how it should work?", isUser: false))
            }
        }
    }
}

#Preview {
    ZStack {
        AppTheme.backgroundGradient.ignoresSafeArea()
        SkillsView()
    }
}
