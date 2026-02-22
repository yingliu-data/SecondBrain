import SwiftUI

struct ChoiceButtonsView: View {
    let choices: [String]
    let onSelect: (String) -> Void

    var body: some View {
        VStack(spacing: 8) {
            ForEach(choices, id: \.self) { choice in
                Button {
                    onSelect(choice)
                } label: {
                    Text(choice)
                        .font(.subheadline)
                        .foregroundColor(.white)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 12)
                }
                .glassEffect(.regular.interactive(), in: RoundedRectangle(cornerRadius: AppTheme.radiusMd))
            }
        }
    }
}
