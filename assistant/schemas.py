"""Tool schema helpers shared by local and connected capabilities."""


def string(description,limit=500,**extra):
    return {'type':'string','description':description,'maxLength':limit,**extra}


def spec(name,description,properties=None,required=None):
    return {'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','properties':properties or {},'required':required or [],'additionalProperties':False}}}


REPEAT=string('once=单次，daily=每天，weekdays=周一至周五，weekly=每周',enum=['once','daily','weekdays','weekly'])
AT=string('完整 ISO 日期时间，包含时区偏移，例如 2026-11-12T15:00:00+08:00',80)
ID=string('从工具结果获得的真实编号，不能编造',64)
