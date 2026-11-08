import createPlotlyComponent from "react-plotly.js/factory";
import Plotly from "plotly.js/lib/core";
import scatter from "plotly.js/lib/scatter";
import type { Data, Layout } from "plotly.js";

Plotly.register([scatter]);
const Plot = createPlotlyComponent(Plotly);

export default function MetricPlot({
  data,
  layout,
}: {
  data: Data[];
  layout: Partial<Layout>;
}) {
  return (
    <Plot
      data={data}
      layout={layout}
      config={{ displayModeBar: false, responsive: true }}
      useResizeHandler
      style={{ width: "100%", height: 228 }}
    />
  );
}
