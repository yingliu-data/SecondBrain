import SwiftUI

struct SettingsView: View {
    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                // Header
                GlassHeader(
                    icon: "gearshape.fill",
                    iconColor: .white,
                    title: "Settings",
                    subtitle: "Customize your experience"
                )

                // Server
                SettingsSection(title: "Server") {
                    SettingsRow(icon: "server.rack", label: "Server URL", detail: "secondbrain.yingliu.site")
                    SettingsRow(icon: "wifi", label: "Connection", detail: "Connected", detailColor: AppTheme.emerald)
                    SettingsRow(icon: "key.fill", label: "API Key", detail: "Configured")
                }

                // Preferences
                SettingsSection(title: "Preferences") {
                    SettingsRow(icon: "waveform", label: "Wake Word", detail: "Off")
                    SettingsRow(icon: "speaker.wave.2.fill", label: "Voice Speed", detail: "Normal")
                }

                // About
                SettingsSection(title: "About") {
                    SettingsRow(icon: "info.circle", label: "Version", detail: "0.5.0")
                    SettingsRow(icon: "doc.text", label: "License", detail: "MIT")
                }
            }
            .padding(16)
        }
    }
}

// MARK: - Section

private struct SettingsSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.caption.weight(.medium))
                .foregroundColor(AppTheme.textSubtle)
                .padding(.horizontal, 4)

            VStack(spacing: 0) {
                content
            }
            .background(AppTheme.glassBg)
            .background(.ultraThinMaterial)
            .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusLg))
            .overlay(
                RoundedRectangle(cornerRadius: AppTheme.radiusLg)
                    .stroke(AppTheme.border, lineWidth: 1)
            )
        }
    }
}

// MARK: - Row

private struct SettingsRow: View {
    let icon: String
    let label: String
    var detail: String? = nil
    var detailColor: Color = AppTheme.textTertiary

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.body)
                .foregroundColor(AppTheme.emerald)
                .frame(width: 36, height: 36)
                .background(Color.black.opacity(0.2))
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .stroke(AppTheme.border, lineWidth: 1)
                )

            Text(label)
                .font(.body)
                .foregroundColor(.white)

            Spacer()

            if let detail {
                Text(detail)
                    .font(.subheadline)
                    .foregroundColor(detailColor)
            }

            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundColor(AppTheme.textTertiary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(AppTheme.border)
                .frame(height: 0.5)
                .padding(.leading, 60)
        }
    }
}

#Preview {
    ZStack {
        AppTheme.backgroundGradient.ignoresSafeArea()
        SettingsView()
    }
}
