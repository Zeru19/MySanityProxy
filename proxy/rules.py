from dataclasses import dataclass, field


@dataclass
class BuiltinRule:
    name: str
    category: str
    pattern: str
    preserve_prefix: int = 0


BUILTIN_RULES: list[BuiltinRule] = [
    BuiltinRule(
        name="居民身份证",
        category="个人身份",
        pattern=r"[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]",
    ),
    BuiltinRule(
        name="护照号",
        category="个人身份",
        pattern=r"(?:[EeGg]\d{8}|[A-Za-z]{2}\d{7})",
    ),
    BuiltinRule(
        name="姓名",
        category="个人身份",
        pattern=r"(?<=(?:被告人|原告人|申请人|被申请人|当事人|委托人|代理人|甲方|乙方|姓名[：:]\s{0,2}))[一-龥]{2,4}",
    ),
    BuiltinRule(
        name="手机号",
        category="联系方式",
        pattern=r"(?<!\d)1[3-9]\d{9}(?!\d)",
        preserve_prefix=3,
    ),
    BuiltinRule(
        name="固定电话",
        category="联系方式",
        pattern=r"0\d{2,3}[-\s]?\d{7,8}",
    ),
    BuiltinRule(
        name="电子邮箱",
        category="联系方式",
        pattern=r"[\w.\-]+@[\w.\-]+\.\w+",
    ),
    BuiltinRule(
        name="银行卡号",
        category="金融信息",
        pattern=r"(?<!\d)\d{16,19}(?!\d)",
    ),
    BuiltinRule(
        name="统一社会信用代码",
        category="机构信息",
        pattern=r"[0-9A-HJ-NP-RT-UW-Y]{18}",
    ),
    BuiltinRule(
        name="案件编号",
        category="司法信息",
        pattern=r"[（(]\d{4}[）)][^\d]{1,10}第\d+[号卷]",
    ),
    BuiltinRule(
        name="车牌号",
        category="其他",
        pattern=r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁夏][A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6}",
    ),
]
