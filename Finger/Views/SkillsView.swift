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

                // Skill cards
                ForEach(Array(skills.enumerated()), id: \.element.id) { index, _ in
                    SkillCard(skill: $skills[index])
                }
            }
            .padding(16)
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

#Preview {
    ZStack {
        AppTheme.backgroundGradient.ignoresSafeArea()
        SkillsView()
    }
}
