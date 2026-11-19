import { PREDICT_PROPERTY_CATALOG, PREDICT_PROPERTY_GROUPS } from "../../constants/predictableProperties";

/** Concise in-page help shown in the module dock. */
export function HomopolymerPredictionHelp() {
  return (
    <div className="np-hp-help">
      <section>
        <h3>输入结构</h3>
        <p>可以画结构、在下方粘贴 SMILES、导入结构图片，或从“我的结构”载入。普通 SMILES 和带 <code>*</code> 连接点的重复单元都可以预测，重复单元建议用两个 <code>*</code> 标出连接点。</p>
      </section>
      <section>
        <h3>预测内容</h3>
        <dl>
          {PREDICT_PROPERTY_GROUPS.map((group) => (
            <div key={group.key}>
              <dt>{group.label}</dt>
              <dd>
                {PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group.key)
                  .map((property) => `${property.label}（${property.unit}）`).join("、")}
              </dd>
            </div>
          ))}
        </dl>
      </section>
      <section>
        <h3>运行与对比</h3>
        <ul>
          <li>点“运行预测”，或按 <kbd>Ctrl</kbd> + <kbd>Enter</kbd>（Mac 上为 <kbd>⌘</kbd> + <kbd>Enter</kbd>）。</li>
          <li>从第二次运行起，结果会自动与上一次对比；可以在“对比”里改选本次会话更早的运行，或选择不对比。变化超过 20% 的项加粗显示。</li>
          <li>“本次会话的运行”最多保留 10 次，点一条可恢复当时的结构和结果。刷新页面后清空。</li>
        </ul>
      </section>
      <section>
        <h3>导出</h3>
        <p>“下载 CSV”保留模型输出的原始数值；“复制表格”可以直接粘贴到 Excel 等表格软件。</p>
      </section>
      <section>
        <h3>结果说明</h3>
        <p>这些是模型预测值，用于研究辅助，需要结合适用范围、已有数据和实验验证判断。</p>
      </section>
    </div>
  );
}
