import { PREDICT_PROPERTY_CATALOG, PREDICT_PROPERTY_GROUPS } from "../../constants/predictableProperties";
import { ChannelBadge } from "./channelIcons";
import { STRUCTURE_FACTS } from "./structureFacts";

/** Concise in-page help shown in the module dock. */
export function HomopolymerPredictionHelp({ structureFactsId }: { structureFactsId?: string }) {
  return (
    <div className="np-hp-help">
      <section>
        <h3>输入结构</h3>
        <p>可以画结构、在下方粘贴 SMILES、导入结构图片，或从“我的结构”载入。普通 SMILES 和带 <code>*</code> 连接点的重复单元都可以预测，重复单元建议用两个 <code>*</code> 标出连接点。</p>
      </section>
      <section id={structureFactsId} className="np-hp-help__facts">
        <h3>结构信息</h3>
        <p>画板下方的一行数值由 RDKit 根据当前结构直接计算，用来检查结构、帮助解读结果，不是模型预测值。连接点 <code>*</code> 不计入原子数和分子式。</p>
        <dl>
          {STRUCTURE_FACTS.map((fact) => (
            <div key={fact.key}>
              <dt>{fact.label}{fact.unit ? <span>（{fact.unit}）</span> : null}</dt>
              <dd>{fact.explanation}</dd>
            </div>
          ))}
        </dl>
        <p>这些只是常见趋势，同一结构的实际性能还取决于聚合度、结晶度、加工条件等。</p>
      </section>
      <section>
        <h3>预测内容</h3>
        <dl>
          {PREDICT_PROPERTY_GROUPS.map((group) => (
            <div key={group.key} className={`is-${group.key}`}>
              <dt><ChannelBadge group={group.key} />{group.label}</dt>
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
        <p>每行中间的刻度条显示读数在一个固定显示区间里的位置，鼠标移到该行时显示区间两端。实心点是本次结果，空心圈是对比的那次运行；超出区间的读数停在端点并带箭头。断裂伸长率和三种渗透率用对数刻度。这些区间只为方便比较，不代表数据库里的分布。</p>
      </section>
    </div>
  );
}
