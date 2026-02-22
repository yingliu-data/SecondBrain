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
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Image(systemName: "bolt.fill")
                            .font(.title2)
                            .foregroundColor(AppTheme.emerald)
                        Text("Skills")
                            .font(.title2.weight(.semibold))
                            .foregroundColor(.white)
                    }
                    Text("Manage your assistant's capabilities")
                        .font(.subheadline)
                        .foregroundColor(AppTheme.textTertiary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
                .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))

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

                        VStack(alignment: .leading, spacing: 2) {
                            Text("Add New Skill")
                                .font(.body.weight(.medium))
                                .foregroundColor(.white)
                            Text("Chat to configure a custom skill")
                                .font(.caption)
                                .foregroundColor(AppTheme.textTertiary)
                        }
                        Spacer()
                    }
                    .padding(16)
                    .glassEffect(.regular.interactive(), in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))
                }
                .buttonStyle(.plain)

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

private struct SkillCard: View {
    @Binding var skill: SkillItem

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: skill.icon)
                .font(.title3)
                .foregroundColor(AppTheme.emerald)
                .frame(width: 48, height: 48)
                .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusMd))

            VStack(alignment: .leading, spacing: 2) {
                Text(skill.name)
                    .font(.body.weight(.medium))
                    .foregroundColor(.white)
                Text(skill.description)
                    .font(.caption)
                    .foregroundColor(AppTheme.textTertiary)
            }

            Spacer()

            EmeraldToggle(isOn: $skill.enabled)
        }
        .padding(16)
        .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))
    }
}

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

struct AddSkillView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var client = AssistantClient()
    @State private var input = ""
    @State private var messages: [(id: UUID, text: String, isUser: Bool)] = [
        (id: UUID(), text: "Hi! I'll help you add a new skill. What would you like your AI assistant to be able to do?", isUser: false)
    ]
    @State private var scrollTask: Task<Void, Never>?
    @FocusState private var isFocused: Bool

    var body: some View {
        ZStack {
            AppTheme.backgroundGradient.ignoresSafeArea()

            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    Button { dismiss() } label: {
                        Image(systemName: "chevron.left")
                            .font(.body.weight(.semibold))
                            .foregroundColor(.white)
                            .frame(width: 36, height: 36)
                            .glassEffect(in: RoundedRectangle(cornerRadius: 10))
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
                            .foregroundColor(AppTheme.textTertiary)
                    }
                    Spacer()
                }
                .padding(16)
                .glassEffect()

                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            ForEach(messages, id: \.id) { msg in
                                HStack {
                                    if msg.isUser { Spacer(minLength: 60) }
                                    Text(msg.text)
                                        .font(.subheadline)
                                        .padding(12)
                                        .foregroundColor(msg.isUser ? Color(hex: "111827") : .white)
                                        .background {
                                            if msg.isUser {
                                                Color.white
                                                    .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusLg))
                                            }
                                        }
                                        .if(!msg.isUser) { view in
                                            view.glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))
                                        }
                                    if !msg.isUser { Spacer(minLength: 60) }
                                }
                                .id(msg.id)
                            }
                            if client.isProcessing && !client.currentResponse.isEmpty {
                                HStack {
                                    Text(client.currentResponse)
                                        .font(.subheadline)
                                        .padding(12)
                                        .foregroundColor(.white)
                                        .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))
                                    Spacer(minLength: 60)
                                }
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
                        if let last = messages.last {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }

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
                    }
                    .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty || client.isProcessing)
                    .opacity(input.trimmingCharacters(in: .whitespaces).isEmpty ? 0.5 : 1.0)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .glassEffect()
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
        let prompt = "[Skill Creation] Help me create a new custom skill for my AI assistant. Guide me step by step to define: name, description, tools, and parameters. User says: \(text)"
        Task {
            await client.send(message: prompt)
            withAnimation(.spring(duration: 0.3)) {
                messages.append((id: UUID(), text: client.currentResponse, isUser: false))
            }
        }
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
