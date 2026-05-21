import SwiftUI

struct WarningBanner: View {
    let warning: HostCompatibilityWarning

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(.yellow)
            VStack(alignment: .leading) {
                Text(warning.title)
                    .font(.body.weight(.medium))
                Text(warning.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(Color.yellow.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

struct SummaryGrid: View {
    let rows: [(String, String)]

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                GridRow {
                    Text(row.0).foregroundStyle(.secondary)
                    Text(row.1)
                        .lineLimit(2)
                        .truncationMode(.middle)
                }
            }
        }
        .font(.caption)
    }
}

struct StageLine: View {
    let stage: OperationStageState

    var body: some View {
        HStack(spacing: 8) {
            Text(stage.stage)
                .font(.system(.caption, design: .monospaced))
            if let description = stage.description {
                Text(description)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
