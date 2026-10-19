"""Product tools: search, detail, recommendation and inventory."""

from pydantic import BaseModel, Field

from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext
from app.tools.deps import RepositoryBackedTool


class SearchInput(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=80,
        description="商品原名或核心品类词，如「T恤」「降噪耳机」。多个关键词可用空格或斜线分隔，需同时匹配；不要填 sku 或订单号",
    )


class SkuInput(BaseModel):
    sku: str = Field(
        min_length=2,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="商品 SKU，如 SKU-A1；不要填商品中文名或 order_id",
    )


class RecommendInput(BaseModel):
    category: str | None = Field(
        default=None,
        max_length=32,
        description="可选品类，如「数码」；不要填 sku",
    )
    query: str = Field(
        default="",
        max_length=80,
        description="可选需求描述，如「送女朋友的礼物」；可与 category 二选一",
    )
    max_price_cents: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000,
        description="预算上限，单位分；100元以内必须填10000，不是100。未提供预算才省略。",
    )
    in_stock_only: bool = Field(
        default=True, description="仅返回库存>0的商品；用户要求有货时保持true"
    )


class SearchProductsTool(RepositoryBackedTool, Tool):
    name = "search_products"
    description = (
        "按关键词搜索商品目录，最多返回约 5 条匹配商品。"
        "何时用：用户给出具体商品名、品类词或属性词要找货，如「无线耳机」「跑步鞋」。"
        "何时不用：已知 sku 用 get_product_detail；「推荐/不知道买什么」用 recommend_products；政策问题用 search_knowledge。"
        "优先使用 search_products 当有明确关键词；模糊需求或推荐语气则用 recommend_products。"
        "参数 query：1–80 字搜索词，不是 sku。例 query=「降噪耳机」，不要填 SKU-A1 或 ORD1001。"
        "返回 products[]：sku、name、category、price_cents（分）、inventory；后续详情用返回的 sku 调 get_product_detail。"
        "没有匹配时可用更短的核心品类词再查一次；不要猜SKU或把空结果说成商品无库存。"
    )
    risk = ToolRisk.READ
    input_model = SearchInput

    async def execute(self, parsed: SearchInput, context: ToolContext) -> ToolResult:
        products = await self.repository.search_products(parsed.query)
        return ToolResult(
            name=self.name,
            success=True,
            output={
                "products": [p.model_dump() for p in products],
                "match_status": "matched" if products else "no_match",
                "notice": "无匹配仅代表本次搜索未命中，不表示商品不存在或无库存；可缩短核心词再查。",
            },
        )


class GetProductDetailTool(RepositoryBackedTool, Tool):
    name = "get_product_detail"
    description = (
        "按 SKU 查询已录入的目录详情：名称、品类、价格、库存、标签、描述。未录入完整技术规格。"
        "何时用：已知 sku，问规格、价格、介绍、这件是什么。"
        "何时不用：还不知道 sku 时先 search_products；只要库存数量用 check_inventory；保修条款用 get_warranty。"
        "优先：仅问有没有货 → check_inventory；还要价格/规格/介绍 → 本工具（已含 inventory）。"
        "参数 sku：2–32 位字母数字_-，例 SKU-A1；不要把商品中文名或 order_id 当 sku。"
        "返回 sku、name、category、price_cents（分）、inventory、tags、description。"
    )
    risk = ToolRisk.READ
    input_model = SkuInput

    async def execute(self, parsed: SkuInput, context: ToolContext) -> ToolResult:
        product = await self.repository.get_product(parsed.sku)
        if product is None:
            raise InvalidRequestError(f"product not found: {parsed.sku}")
        return ToolResult(
            name=self.name,
            success=True,
            output={
                **product.model_dump(),
                "technical_specifications": "not_recorded",
                "notice": "只能确认目录已录入的属性；续航、蓝牙版本、照片定制能力等未录入，必须说明未知，不能凭常识补造。",
            },
        )


class RecommendProductsTool(RepositoryBackedTool, Tool):
    name = "recommend_products"
    description = (
        "按价格上限、库存和可选品类筛选最多3个真实商品，适合预算/礼物等模糊需求。只读，不会下单。"
        "何时用：用户说推荐、随便看看、按场景/预算选、不知道买哪款。"
        "何时不用：已有明确搜索词用 search_products；已知 sku 用 get_product_detail。"
        "优先使用 search_products 当用户给出具体商品关键词；否则用本工具。"
        "参数max_price_cents以分计：100元以内填10000；in_stock_only默认true。"
        "category可选目录品类，如数码、服饰、家居；不限制品类时省略。query为软需求文本，可空，不是硬关键词条件。"
        "礼物等文字未匹配时仍返回满足价格库存品类的候选，matching_mode会说明只是约束匹配；不得说候选已满足个人喜好。"
        "不要用search_products反复搜索‘100元以内有货礼物’，请直接使用本工具的结构化筛选。"
    )
    risk = ToolRisk.READ
    input_model = RecommendInput

    async def execute(self, parsed: RecommendInput, context: ToolContext) -> ToolResult:
        products, mode = await self.repository.recommend_products(
            query=parsed.query,
            category=parsed.category,
            max_price_cents=parsed.max_price_cents,
            in_stock_only=parsed.in_stock_only,
        )
        return ToolResult(
            name=self.name,
            success=True,
            output={
                "products": [p.model_dump() for p in products],
                "applied_filters": {
                    "category": parsed.category,
                    "max_price_cents": parsed.max_price_cents,
                    "in_stock_only": parsed.in_stock_only,
                },
                "matching_mode": mode,
                "notice": "候选已按明确价格、库存、品类筛选；需求文本未匹配时只是约束候选，适不适合作礼物仍由用户选择；不会下单。",
            },
        )


class CheckInventoryTool(RepositoryBackedTool, Tool):
    name = "check_inventory"
    description = (
        "按 SKU 只查询库存数量，不含价格与规格。"
        "何时用：已知 sku，只问有货吗、库存多少。"
        "何时不用：还要价格/规格/介绍用 get_product_detail；不知 sku 先 search_products。"
        "优先：仅库存 → 本工具；同时要价格或详情 → get_product_detail（无需再调本工具）。"
        "参数 sku：例 SKU-A1，不是商品中文名。"
        "返回 {sku, inventory}；0 表示无货，不要编造补货日期。"
    )
    risk = ToolRisk.READ
    input_model = SkuInput

    async def execute(self, parsed: SkuInput, context: ToolContext) -> ToolResult:
        product = await self.repository.get_product(parsed.sku)
        if product is None:
            raise InvalidRequestError(f"product not found: {parsed.sku}")
        return ToolResult(
            name=self.name,
            success=True,
            output={"sku": product.sku, "inventory": product.inventory},
        )
