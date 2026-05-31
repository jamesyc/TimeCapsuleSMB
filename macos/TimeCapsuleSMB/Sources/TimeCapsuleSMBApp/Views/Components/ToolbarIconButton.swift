import SwiftUI

struct ToolbarIconButton: View {
    let title: String
    let systemImage: String
    var disabled = false
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button {
            action()
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 13, weight: .medium))
                .frame(width: 28, height: 28)
                .background {
                    Circle()
                        .fill(isHovered && !disabled ? Color.primary.opacity(0.10) : Color.clear)
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .help(title)
        .accessibilityLabel(title)
        .onHover { hovering in
            isHovered = hovering
        }
    }
}
