import SwiftUI

enum AppTheme {
    // Backgrounds
    static let backgroundGradient = LinearGradient(
        colors: [Color(hex: "0f172a"), Color(hex: "1e293b"), Color(hex: "171717")],
        startPoint: .top, endPoint: .bottom
    )
    static let glassBg = Color.black.opacity(0.3)

    // Accents
    static let accentGradient = LinearGradient(
        colors: [Color(hex: "10b981"), Color(hex: "0d9488")],
        startPoint: .leading, endPoint: .trailing
    )
    static let emerald = Color(hex: "10b981")

    // Text
    static let textPrimary = Color.white
    static let textSecondary = Color.white.opacity(0.6)
    static let textTertiary = Color.white.opacity(0.4)
    static let textSubtle = Color(hex: "a7f3d0").opacity(0.7)

    // Borders
    static let border = Color.white.opacity(0.1)

    // Radii
    static let radiusLg: CGFloat = 24
    static let radiusMd: CGFloat = 16
    static let radiusSm: CGFloat = 12
}

extension Color {
    init(hex: String) {
        let scanner = Scanner(string: hex)
        var rgb: UInt64 = 0
        scanner.scanHexInt64(&rgb)
        self.init(
            red: Double((rgb >> 16) & 0xFF) / 255,
            green: Double((rgb >> 8) & 0xFF) / 255,
            blue: Double(rgb & 0xFF) / 255
        )
    }
}

// MARK: - Shared Components

struct GlassHeader: View {
    let icon: String
    var iconColor: Color = AppTheme.emerald
    let title: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.title2)
                    .foregroundColor(iconColor)
                Text(title)
                    .font(.title2.weight(.semibold))
                    .foregroundColor(.white)
            }
            Text(subtitle)
                .font(.subheadline)
                .foregroundColor(AppTheme.textSubtle)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(AppTheme.glassBg)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: AppTheme.radiusLg))
        .overlay(
            RoundedRectangle(cornerRadius: AppTheme.radiusLg)
                .stroke(AppTheme.border, lineWidth: 1)
        )
    }
}
